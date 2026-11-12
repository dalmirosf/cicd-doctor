"""Builders for domain objects with sensible defaults. Override fields with keyword arguments."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    Evidence,
    FailedJob,
    Failure,
    FailureStatus,
    JobRef,
    LogExcerpt,
    LogLine,
    RunRef,
    Signature,
)

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

_RUN = RunRef(
    installation_id=1,
    repo_id=100,
    repo_full_name="acme/widgets",
    run_id=5000,
    run_attempt=1,
    workflow_id=7,
    workflow_name="CI",
    head_sha="a" * 40,
    head_branch="feature/x",
    head_repo_full_name="acme/widgets",
    event="pull_request",
    is_fork=False,
    pr_number=12,
)


def make_run_ref(**changes: Any) -> RunRef:
    return _RUN.model_copy(update=changes)


def make_failure(
    *,
    run: RunRef | None = None,
    job_id: int = 1,
    signature_hash: str = "f" * 64,
    embedding: list[float] | None = None,
    status: FailureStatus = FailureStatus.OPEN,
    failure_id: UUID | None = None,
) -> Failure:
    run = run or make_run_ref()
    job_ref = JobRef(run=run, job_id=job_id, job_name="tests")
    return Failure(
        id=failure_id or uuid4(),
        job=FailedJob(
            job=job_ref,
            step_name="Run pytest",
            step_number=5,
            started_at=T0,
            completed_at=T0,
            runner_os="Linux",
        ),
        excerpt=LogExcerpt(
            job=job_ref,
            step_name="Run pytest",
            lines=[LogLine(number=10, text="ModuleNotFoundError: No module named 'requests'")],
            omitted=[],
            error_lines=[10],
            token_count=12,
            truncated=False,
            redactions=0,
        ),
        signature=Signature(text="CI\ntests\nRun pytest\nPY-MODNF", hash=signature_hash),
        embedding=embedding,
        status=status,
        fix_commit_sha=None,
    )


def make_diagnosis(**changes: Any) -> Diagnosis:
    base = Diagnosis(
        category=Category.DEPENDENCY,
        probable_cause="The 'requests' package is imported but not declared.",
        evidence=[Evidence(line=10, text="No module named 'requests'")],
        suggested_fix="Add requests to [project.dependencies].",
        confidence=0.9,
        source="llm",
    )
    return base.model_copy(update=changes)
