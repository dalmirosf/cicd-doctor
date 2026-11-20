"""The ``discover`` and ``capture-public`` commands (D-101), kept apart from ``cli.py``.

Both read GitHub through ``gh`` (``GET`` only) and print ids, counts and reasons from a fixed
vocabulary: never the text of a log, a workflow name or any other third-party string.
"""

import argparse
from pathlib import Path
from typing import Any

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from evals.runner.capture import Gh, run_gh
from evals.runner.public_capture import (
    DEFAULT_PUBLIC_MANIFEST,
    PUBLIC_DIR,
    capture_public,
    load_public_manifest,
)
from evals.runner.public_common import MAX_API_CALLS, BudgetedGh
from evals.runner.public_discover import DEFAULT_CANDIDATES, discover, render_candidates

PUBLIC_COMMANDS = ("discover", "capture-public")


def add_public_commands(commands: Any) -> None:
    find = commands.add_parser(
        "discover",
        help="list failed runs of a public repository and pair them with their fix (uses gh)",
        description="Read only. Writes candidates.yaml: the pairs that pass the filters and why "
        "each of the others was discarded. Nothing is a case until it is in the manifest.",
    )
    find.add_argument("--repo", action="append", required=True, help="owner/name (repeatable)")
    find.add_argument("--since", default="90d", help="how far back to look, in days (default 90d)")
    find.add_argument("--limit", type=int, help="stop after this many candidates per repository")
    find.add_argument("--exclude", action="append", default=[], help="extra workflow name term")
    find.add_argument("--out", type=Path, default=DEFAULT_CANDIDATES)
    _budget(find)

    cap = commands.add_parser(
        "capture-public",
        help="write the public cases of the manifest (uses gh)",
        description="The manifest is the only source of truth. A case that fails a check is "
        "discarded with its reason and the batch goes on.",
    )
    cap.add_argument("--manifest", type=Path, default=DEFAULT_PUBLIC_MANIFEST)
    cap.add_argument("--out-dir", type=Path, default=PUBLIC_DIR)
    cap.add_argument("--case", action="append", help="capture only this id (repeatable)")
    _budget(cap)


def _budget(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=MAX_API_CALLS,
        help=f"stop after this many GitHub calls (default {MAX_API_CALLS}); it also stops when "
        "the rate limit is down to 200",
    )


def run_public(args: argparse.Namespace, gh: Gh | None = None) -> int:
    budget = BudgetedGh(gh or run_gh, max_calls=args.max_api_calls)
    if args.command == "discover":
        return _discover(args, budget)
    return _capture(args, budget)


def _days(value: str) -> int:
    text = value.strip().lower().removesuffix("d")
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"--since must be a number of days such as 90d, got {value!r}")
    return int(text)


def _discover(args: argparse.Namespace, budget: BudgetedGh) -> int:
    results = []
    for repo in args.repo:
        found = discover(
            repo,
            gh=budget,
            since_days=_days(args.since),
            limit=args.limit,
            exclude=args.exclude,
            calls=lambda: budget.calls,
        )
        results.append(found)
        print(
            f"{repo}: {len(found.candidates)} candidates, {len(found.discarded)} discarded, "
            f"{found.api_calls} api calls"
            + (f", stopped: {found.stopped}" if found.stopped else "")
        )
        if found.stopped:
            break
    args.out.write_text(render_candidates(results), encoding="utf-8", newline="\n")
    print(f"candidates: {args.out}")
    return 2 if any(r.stopped for r in results) else 0


def _capture(args: argparse.Namespace, budget: BudgetedGh) -> int:
    manifest = load_public_manifest(args.manifest)
    report = capture_public(
        manifest,
        gh=budget,
        out_dir=args.out_dir,
        counter=TiktokenCounter(),
        only=args.case or (),
    )
    for item in report.written:
        print(
            f"wrote {item.path} (job {item.job_id}, {item.lines} lines, {item.bytes} bytes, "
            f"{item.redactions} redactions, {item.capture_replacements} e-mails/logins, "
            f"{'trimmed' if item.trimmed else 'whole'}, evidence lines {item.evidence_lines})"
        )
        for pattern in item.missing_evidence:
            print(f"warning: {item.id}: an evidence pattern matched no line ({pattern!r})")
    for case_id, reason in report.discarded:
        print(f"discarded {case_id}: {reason}")
    print(
        f"{len(report.written)} written, {len(report.discarded)} discarded, "
        f"{budget.calls} api calls"
    )
    if report.stopped:
        print(f"stopped: {report.stopped}")
        return 2
    return 0
