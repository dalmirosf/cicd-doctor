"""Builders for domain objects with sensible defaults. Override fields with keyword arguments."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    DiagnosisUsage,
    Evidence,
    FailedJob,
    Failure,
    FailureStatus,
    JobRef,
    LogExcerpt,
    LogLine,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    Signature,
    StoredFailure,
    Suggestion,
    WorkflowRun,
    failure_id_for,
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
            conclusion="failure",
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


# --- run attempt analyses (D-69) ----------------------------------------------------------


def make_workflow_run(run: RunRef | None = None) -> WorkflowRun:
    """The run-level data of a ``RunRef``."""
    run = run or make_run_ref()
    return WorkflowRun(
        id=run.run_id,
        repo_id=run.repo_id,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow_name,
        head_sha=run.head_sha,
        head_branch=run.head_branch,
        head_repo_full_name=run.head_repo_full_name,
        event=run.event,
        is_fork=run.is_fork,
        pr_number=run.pr_number,
    )


def make_run_attempt(run: RunRef | None = None, conclusion: str = "failure") -> RunAttempt:
    run = run or make_run_ref()
    return RunAttempt(
        run_id=run.run_id, attempt=run.run_attempt, conclusion=conclusion, completed_at=T0
    )


def make_usage(**changes: Any) -> DiagnosisUsage:
    base = DiagnosisUsage(
        model="gpt-test",
        calls=2,
        input_tokens=1800,
        output_tokens=240,
        latency_ms=2100,
        cost_usd=Decimal("0.001234"),
        failed_calls=1,
    )
    return base.model_copy(update=changes)


def make_rules_diagnosis(**changes: Any) -> Diagnosis:
    """What the rule engine produces: no model, no fallback reason."""
    return make_diagnosis(
        source="rules", confidence=0.85, rule_ids=["PY-MODNF"], evidence_verified=True, **changes
    )


def make_stored_failure(
    *,
    run: RunRef | None = None,
    job_id: int = 1,
    published: Diagnosis | None = None,
    usage: DiagnosisUsage | None = None,
    **failure_changes: Any,
) -> StoredFailure:
    """A failure whose id is ``failure_id_for(...)`` with the diagnosis of an LLM answer.

    Without ``published`` it is a valid LLM diagnosis (confidence 0.855 on purpose: it must
    survive the database exactly) and, without ``usage``, the usage of its two calls.
    """
    run = run or make_run_ref()
    failure = make_failure(
        run=run,
        job_id=job_id,
        failure_id=failure_id_for(run.run_id, run.run_attempt, job_id),
        **failure_changes,
    )
    if published is None:
        published = make_diagnosis(
            model="gpt-test",
            prompt_version="v1",
            confidence=0.855,
            rule_ids=["PY-MODNF"],
            suggestion=Suggestion(
                path="pyproject.toml", start_line=12, end_line=12, replacement='"requests",'
            ),
        )
        usage = usage or make_usage()
    return StoredFailure(
        failure=failure, published=published, rules=make_rules_diagnosis(), usage=usage
    )


def make_run_analysis(
    run: RunRef | None = None,
    items: Sequence[StoredFailure] | None = None,
    *,
    conclusion: str = "failure",
) -> RunAnalysisRecord:
    """The checkpoint of a run attempt. By default three failures, one of each kind:

    1. an LLM diagnosis with its usage;
    2. a rules fallback of a job whose calls were billed (``invalid_output``): the usage is on
       the published rules row;
    3. a rules fallback with no call at all (``fork_policy``).
    """
    run = run or make_run_ref()
    if items is None:
        rules = make_rules_diagnosis()
        items = [
            make_stored_failure(run=run, job_id=1),
            make_stored_failure(
                run=run,
                job_id=2,
                published=rules.model_copy(update={"fallback_reason": "invalid_output"}),
                usage=make_usage(cost_usd=Decimal("0.000900"), failed_calls=0),
                signature_hash="2" * 64,
            ),
            make_stored_failure(
                run=run,
                job_id=3,
                published=rules.model_copy(update={"fallback_reason": "fork_policy"}),
                signature_hash="3" * 64,
            ),
        ]
    return RunAnalysisRecord(
        run=make_workflow_run(run), attempt=make_run_attempt(run, conclusion), items=list(items)
    )
