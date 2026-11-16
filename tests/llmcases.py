"""Builders for the LLM tests: excerpts, requests, jobs and the JSON an LLM would answer with."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from cicd_doctor.core.analysis import JobAnalysis
from cicd_doctor.core.models import (
    Category,
    DiagnosisRequest,
    FailedJob,
    JobRef,
    LogExcerpt,
    LogLine,
    RuleMatch,
)
from tests.factories import make_run_ref

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)

LOG_LINES = [
    "Run uv run python -m labpkg.cli",
    "Traceback (most recent call last):",
    '  File "src/labpkg/cli.py", line 3, in <module>',
    "    import requests",
    "ModuleNotFoundError: No module named 'requests'",
    "##[error]Process completed with exit code 1.",
]


def make_job(**changes: Any) -> FailedJob:
    fields: dict[str, Any] = {
        "job": JobRef(run=make_run_ref(), job_id=1, job_name="build (ubuntu-latest, 3.13)"),
        "conclusion": "failure",
        "step_name": "Run uv run python -m labpkg.cli",
        "step_number": 5,
        "started_at": T0,
        "completed_at": T0 + timedelta(minutes=1),
        "runner_os": "Linux",
    }
    return FailedJob(**(fields | changes))


def make_excerpt(
    lines: list[str] | None = None,
    *,
    first_number: int = 1,
    error_lines: list[int] | None = None,
    omitted: list[tuple[int, int]] | None = None,
    truncated: bool = False,
) -> LogExcerpt:
    texts = LOG_LINES if lines is None else lines
    return LogExcerpt(
        job=make_job().job,
        step_name="Run uv run python -m labpkg.cli",
        lines=[LogLine(number=first_number + i, text=t) for i, t in enumerate(texts)],
        omitted=omitted or [],
        error_lines=error_lines if error_lines is not None else [first_number + 4],
        token_count=10,
        truncated=truncated,
        redactions=0,
    )


def make_hint(rule_id: str = "PY-MODNF", **changes: Any) -> RuleMatch:
    fields: dict[str, Any] = {
        "rule_id": rule_id,
        "category": Category.DEPENDENCY,
        "confidence": 0.9,
        "cause": "The module is not installed.",
        "fix": "Declare it.",
        "evidence_lines": [5],
    }
    return RuleMatch(**(fields | changes))


def make_request(excerpt: LogExcerpt | None = None, **changes: Any) -> DiagnosisRequest:
    fields: dict[str, Any] = {
        "job": make_job(),
        "excerpt": excerpt or make_excerpt(),
        "rule_hints": [],
    }
    return DiagnosisRequest(**(fields | changes))


def answer(**changes: Any) -> dict[str, Any]:
    """A schema-valid answer that quotes line 5 of the default excerpt."""
    fields: dict[str, Any] = {
        "category": "dependency",
        "probable_cause": "The module requests is imported but not installed.",
        "evidence": [{"line": 5, "text": "ModuleNotFoundError: No module named 'requests'"}],
        "suggested_fix": "Declare requests in the project dependencies.",
        "suggestion": None,
        "confidence": 0.9,
    }
    return fields | changes


def raw(**changes: Any) -> str:
    """The raw JSON of ``answer(**changes)``."""
    return json.dumps(answer(**changes))


def analysis_for(case_name: str = "py-modnf") -> "JobAnalysis":
    """The offline analysis (excerpt, rules, signature) of a synthetic log fixture."""
    from cicd_doctor.adapters.memory import CharTokenCounter
    from cicd_doctor.core.analysis import analyze_jobs
    from tests.logcases import load_log_case, select_case_jobs

    case = load_log_case(case_name)
    [job] = select_case_jobs(case)[:1]
    [analysis] = analyze_jobs([(job, case.raw_log(job.job.job_id))], CharTokenCounter())
    return analysis
