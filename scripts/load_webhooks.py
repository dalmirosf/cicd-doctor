"""Load test for ``POST /webhooks/github`` (NFR-01, SPEC §13 P1a).

Sends N signed ``workflow_run`` failure deliveries (each with its own delivery id and run id) to a
running API and reports the acknowledgement latency. Exits with 1 unless every delivery got 202
and p99 is under the limit (1 s by default).

    uv run cicd-doctor api                                  # terminal 1
    uv run python scripts/load_webhooks.py --cleanup        # terminal 2

The webhook secret and DATABASE_URL come from the settings (.env); neither is printed. The run ids
start at 9e12, far above GitHub's, and ``--cleanup`` deletes the deliveries and jobs the run
created.
"""

import argparse
import copy
import hashlib
import hmac
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg

from cicd_doctor.app.settings import Settings

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "webhooks"
    / "workflow_run_completed_failure.json"
)
RUN_ID_BASE = 9_000_000_000_000


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty sequence."""
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def send(url: str, secret: bytes, payload: dict[str, Any]) -> tuple[str, int, float]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    delivery = str(uuid.uuid4())
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    return delivery, status, time.perf_counter() - started


def cleanup(database_url: str, deliveries: list[str]) -> int:
    libpq = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq, connect_timeout=10) as conn:
        removed = conn.execute(
            "DELETE FROM jobs WHERE delivery_id = ANY(%s::uuid[])", (deliveries,)
        ).rowcount
        conn.execute(
            "DELETE FROM webhook_deliveries WHERE delivery_id = ANY(%s::uuid[])", (deliveries,)
        )
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/github")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--max-p99", type=float, default=1.0, help="seconds (default: 1.0)")
    parser.add_argument("--cleanup", action="store_true", help="delete what this run created")
    args = parser.parse_args(argv)

    settings = Settings()
    if settings.github_webhook_secret is None:
        print("GITHUB_WEBHOOK_SECRET is not set (.env)", file=sys.stderr)
        return 2
    secret = settings.github_webhook_secret.get_secret_value().encode("utf-8")
    template = json.loads(FIXTURE.read_text(encoding="utf-8"))
    base = RUN_ID_BASE + random.randrange(1_000_000) * 1_000  # a fresh range for every run

    def payload(index: int) -> dict[str, Any]:
        data: dict[str, Any] = copy.deepcopy(template)
        data["workflow_run"]["id"] = base + index
        return data

    # Warm-up: open the API's database connection before measuring.
    urllib.request.urlopen(args.url.rsplit("/webhooks", 1)[0] + "/ready", timeout=15).close()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda i: send(args.url, secret, payload(i)), range(args.count)))
    wall = time.perf_counter() - started

    latencies = sorted(seconds for _, _, seconds in results)
    statuses = Counter(status for _, status, _ in results)
    p50, p95, p99 = (percentile(latencies, p) for p in (50, 95, 99))
    print(f"deliveries: {args.count}  concurrency: {args.concurrency}  wall: {wall:.2f} s")
    print(f"statuses:   {dict(sorted(statuses.items()))}")
    print(
        f"latency ms: p50 {p50 * 1000:.1f}  p95 {p95 * 1000:.1f}  p99 {p99 * 1000:.1f}  "
        f"max {latencies[-1] * 1000:.1f}"
    )

    if args.cleanup:
        deliveries = [delivery for delivery, _, _ in results]
        removed = cleanup(settings.database_url.get_secret_value(), deliveries)
        print(f"cleanup:    {removed} jobs and their deliveries deleted")

    ok = statuses == Counter({202: args.count}) and p99 < args.max_p99
    print("result:     " + ("PASS" if ok else f"FAIL (need all 202 and p99 < {args.max_p99} s)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
