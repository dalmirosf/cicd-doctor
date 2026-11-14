"""``python -m evals run``: run variants over the cases and write a report (SPEC §11.3)."""

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from cicd_doctor.core.ports import TokenCounter
from evals.runner.cases import load_cases
from evals.runner.metrics import compute
from evals.runner.report import Report, VariantRun
from evals.runner.variants import ALL_VARIANTS, NOT_YET, VARIANTS


class EvalError(Exception):
    """A problem with the request (unknown variant, no cases): reported, not a crash."""


def resolve_variants(requested: str) -> tuple[list[str], dict[str, str]]:
    """``(variants to run, skipped variant -> reason)`` for ``--variant``."""
    if requested == "all":
        runnable = [v for v in ALL_VARIANTS if v in VARIANTS]
        return runnable, {v: NOT_YET[v] for v in ALL_VARIANTS if v not in VARIANTS}
    if requested in VARIANTS:
        return [requested], {}
    if requested in NOT_YET:
        raise EvalError(f"variant {requested!r} is not available yet: it {NOT_YET[requested]}")
    raise EvalError(f"unknown variant {requested!r}; choose from {', '.join(ALL_VARIANTS)} or all")


def run_evals(
    variant: str,
    sources: Sequence[str],
    cases_dir: Path,
    counter: TokenCounter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    git_sha: Callable[[], str] | None = None,
) -> Report:
    variants, skipped = resolve_variants(variant)
    cases = load_cases(cases_dir, sources)
    if not cases:
        raise EvalError(
            f"no cases for source(s) {', '.join(sources)} under {cases_dir}; "
            "capture lab cases with `python -m evals capture` (see lab/README.md)"
        )
    runs = []
    for name in variants:
        results = [VARIANTS[name](case, counter) for case in cases]
        runs.append(VariantRun(variant=name, metrics=compute(results), results=results))
    return Report(
        generated_at=now(),
        git_sha=(git_sha or current_git_sha)(),
        sources=list(sources),
        runs=runs,
        skipped=skipped,
    )


def current_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    return result.stdout.strip() or "nogit"
