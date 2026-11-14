"""Pick the failed jobs of a run attempt and the failed step of each (FR-06, SPEC §7.1 step 4)."""

from collections.abc import Sequence
from typing import Literal

from cicd_doctor.core.models import FailedJob, JobRef, RunRef, WorkflowJob, WorkflowStep

DIAGNOSED_JOB_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out"})
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


def select_failed_jobs(run: RunRef, jobs: Sequence[WorkflowJob], limit: int) -> list[FailedJob]:
    """Jobs with conclusion ``failure`` or ``timed_out``, earliest ``completed_at`` first (ties by
    job id), at most ``limit`` of them (``LOG_MAX_JOBS``)."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    candidates = [
        job
        for job in jobs
        if job.conclusion in DIAGNOSED_JOB_CONCLUSIONS and job.completed_at is not None
    ]
    candidates.sort(key=lambda job: (job.completed_at, job.job_id))
    return [_failed_job(run, job) for job in candidates[:limit]]


def _failed_job(run: RunRef, job: WorkflowJob) -> FailedJob:
    assert job.completed_at is not None  # filtered by the caller
    conclusion: Literal["failure", "timed_out"] = (
        "timed_out" if job.conclusion == "timed_out" else "failure"
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
