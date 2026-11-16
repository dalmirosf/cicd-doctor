"""Pick the failed jobs of a run attempt and the failed step of each (FR-06, SPEC §7.1 step 4)."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from cicd_doctor.core.models import FailedJob, JobRef, RunRef, WorkflowJob, WorkflowStep

DIAGNOSED_JOB_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out"})
# GitHub ends a job that exceeds its ``timeout-minutes`` as ``cancelled`` and only says so in an
# annotation of its check run (D-73).
JOB_TIMEOUT_ANNOTATION = "has exceeded the maximum execution time"
MAX_ANNOTATION_JOBS = 10  # cancelled jobs whose annotations are read per run
# A job that timed out has no step with conclusion "failure": the step that was running when the
# limit hit is "cancelled" (or "timed_out" for a step-level timeout).
_FALLBACK_STEP_CONCLUSIONS: frozenset[str] = frozenset({"timed_out", "cancelled"})


def failed_step(job: WorkflowJob) -> WorkflowStep | None:
    """The first step with conclusion ``failure``; else the first ``timed_out`` or ``cancelled``
    one; else None (the whole job log is used)."""
    steps = sorted(job.steps, key=lambda step: step.number)
    for step in steps:
        if step.conclusion == "failure":
            return step
    for step in steps:
        if step.conclusion in _FALLBACK_STEP_CONCLUSIONS:
            return step
    return None


def is_job_timeout(annotations: Iterable[str]) -> bool:
    """True when an annotation of a ``cancelled`` job says it exceeded its time limit (D-73)."""
    return any(JOB_TIMEOUT_ANNOTATION in message.lower() for message in annotations)


def cancelled_jobs(
    jobs: Sequence[WorkflowJob], limit: int = MAX_ANNOTATION_JOBS
) -> list[WorkflowJob]:
    """The ``cancelled`` jobs whose annotations are worth reading, earliest first, at most
    ``limit`` of them."""
    cancelled = [job for job in jobs if job.conclusion == "cancelled" and job.completed_at]
    cancelled.sort(key=lambda job: (job.completed_at, job.job_id))
    return cancelled[:limit]


def select_failed_jobs(
    run: RunRef,
    jobs: Sequence[WorkflowJob],
    limit: int,
    annotations: Mapping[int, Sequence[str]] | None = None,
) -> list[FailedJob]:
    """Jobs with conclusion ``failure`` or ``timed_out``, earliest ``completed_at`` first (ties by
    job id), at most ``limit`` of them (``LOG_MAX_JOBS``).

    ``annotations`` maps a job id to the annotation messages already read; a ``cancelled`` job
    with a timeout annotation counts as ``timed_out`` (D-73). Any other cancelled job is not a
    failure.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    notes = annotations or {}
    candidates = [
        job
        for job in jobs
        if job.completed_at is not None
        and (
            job.conclusion in DIAGNOSED_JOB_CONCLUSIONS
            or (job.conclusion == "cancelled" and is_job_timeout(notes.get(job.job_id, ())))
        )
    ]
    candidates.sort(key=lambda job: (job.completed_at, job.job_id))
    return [_failed_job(run, job) for job in candidates[:limit]]


def _failed_job(run: RunRef, job: WorkflowJob) -> FailedJob:
    assert job.completed_at is not None  # filtered by the caller
    conclusion: Literal["failure", "timed_out"] = (
        "failure" if job.conclusion == "failure" else "timed_out"
    )
    step = failed_step(job)
    return FailedJob(
        job=JobRef(run=run, job_id=job.job_id, job_name=job.name),
        conclusion=conclusion,
        step_name=step.name if step else None,
        step_number=step.number if step else None,
        started_at=job.started_at or job.completed_at,
        completed_at=job.completed_at,
        runner_os=job.runner_os,
        step_started_at=step.started_at if step else None,
        step_completed_at=step.completed_at if step else None,
    )
