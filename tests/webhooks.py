"""Helpers for webhook tests: load the synthetic payloads and sign them like GitHub does."""

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.core.ports import JobQueue
from tests.factories import T0

FIXTURES = Path(__file__).parent / "fixtures" / "webhooks"
SECRET = b"test-webhook-secret"  # not a real secret: only these tests use it


def load(name: str) -> dict[str, Any]:
    """A fresh copy of ``tests/fixtures/webhooks/<name>.json``, safe to modify."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def memory_dependencies(
    *,
    queue: JobQueue | None = None,
    secrets: tuple[bytes, ...] = (SECRET,),
    ready: bool = True,
) -> ApiDependencies:
    """API dependencies backed by an in-memory queue (unit tests)."""

    async def is_ready() -> bool:
        return ready

    return ApiDependencies(
        queue=queue or InMemoryJobQueue(FakeClock(T0)),
        webhook_secrets=secrets,
        ready=is_ready,
        clock=FakeClock(T0),
    )


def headers(
    event: str, body: bytes, *, delivery: str | None = None, secret: bytes = SECRET
) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid4()),
        "X-Hub-Signature-256": sign(body, secret),
        "Content-Type": "application/json",
    }
