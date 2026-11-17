"""The outcome state machine (SPEC §7.2, D-83): when a later success resolves an open failure.

Pure functions, no I/O. Runs are ordered by ``(run_id, run_attempt)``: GitHub run ids grow with
creation and the same key orders publications (D-69).

A failure is resolved by a success of the same repository, workflow and branch key (head
repository plus head branch, so forks never mix) that comes *after* it:

* the same ``run_id``, a later attempt: the code did not change, so the failure was flaky;
* another ``run_id`` with another ``head_sha`` on a named branch: a commit fixed it.

Everything else leaves the failure open: the same ``head_sha`` in another run (another event can
run other test code, so it is neither flaky nor a fix), an empty ``head_branch`` (tags, schedules:
no branch to compare), a failure of a run that is *newer* than the success (the branch is still
red) and any failure that is not ``open``.
"""

from datetime import datetime, timedelta
from uuid import UUID

from cicd_doctor.core.models import (
    DomainModel,
    Failure,
    FailureStatus,
    ResolvedBy,
    RunRef,
    SuccessRef,
)

STALE_AFTER = timedelta(days=30)  # an open failure older than this becomes ``stale`` (D-24)


class Outcome(DomainModel):
    """A successful attempt, as the webhook reports it."""

    success: RunRef
    completed_at: datetime


class Resolution(DomainModel):
    failure_id: UUID
    status: FailureStatus
    fix_commit_sha: str | None
    # The success that resolved it, stored with the status (D-84). None only for a resolution
    # built by hand.
    resolved_by: ResolvedBy | None = None


def decide(outcome: Outcome, open_failures: list[Failure]) -> list[Resolution]:
    """What a success resolves among the open failures of its branch."""
    success = outcome.success
    reference = SuccessRef(
        run_id=success.run_id, attempt=success.run_attempt, head_sha=success.head_sha
    )
    resolutions: list[Resolution] = []
    for failure in open_failures:
        run = failure.job.job.run
        if (
            run.repo_id != success.repo_id
            or run.workflow_id != success.workflow_id
            or run.branch_key != success.branch_key
        ):
            continue
        resolution = _resolve(failure, reference, success.head_branch)
        if resolution is not None:
            resolutions.append(resolution)
    return resolutions


def decide_late(failure: Failure, later_successes: list[SuccessRef]) -> Resolution | None:
    """The same rule read from the failure: the earliest later success that resolves it.

    ``later_successes`` are the recorded successes of the failure's branch. The caller passes
    only the ones of the same repository, workflow and branch key (the store query does).
    """
    run = failure.job.job.run
    for success in sorted(later_successes, key=lambda s: (s.run_id, s.attempt)):
        resolution = _resolve(failure, success, run.head_branch)
        if resolution is not None:
            return resolution
    return None


def _resolve(
    failure: Failure, success: SuccessRef, success_branch: str | None
) -> Resolution | None:
    if failure.status is not FailureStatus.OPEN:
        return None
    run = failure.job.job.run
    by = ResolvedBy(run_id=success.run_id, attempt=success.attempt)
    if (run.run_id, run.run_attempt) >= (success.run_id, success.attempt):
        return None  # the failure is newer than the success: the branch is still red
    if run.run_id == success.run_id:
        return Resolution(
            failure_id=failure.id,
            status=FailureStatus.RESOLVED_FLAKY,
            fix_commit_sha=None,
            resolved_by=by,
        )
    if run.head_sha == success.head_sha or not success_branch or not run.head_branch:
        return None
    return Resolution(
        failure_id=failure.id,
        status=FailureStatus.RESOLVED_BY_COMMIT,
        fix_commit_sha=success.head_sha,
        resolved_by=by,
    )


def is_stale(created_at: datetime, now: datetime, after: timedelta = STALE_AFTER) -> bool:
    """True once a failure created at ``created_at`` is more than ``after`` old."""
    return now - created_at > after
