"""The outcome state machine (SPEC §12, D-83): when a success resolves a failure, and that the
result does not depend on the order in which failures and successes arrive."""

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cicd_doctor.core.models import Failure, FailureStatus, ResolvedBy, RunRef, SuccessRef
from cicd_doctor.core.outcomes import (
    Outcome,
    Resolution,
    decide,
    decide_late,
    is_stale,
)
from tests.factories import T0, make_failure, make_run_ref

A, B, C = "a" * 40, "b" * 40, "c" * 40
FLAKY = FailureStatus.RESOLVED_FLAKY
FIX = FailureStatus.RESOLVED_BY_COMMIT


def _failure(
    run_id: int = 5000,
    attempt: int = 1,
    sha: str = A,
    job_id: int = 1,
    status: FailureStatus = FailureStatus.OPEN,
    **run_changes: object,
) -> Failure:
    run = make_run_ref(run_id=run_id, run_attempt=attempt, head_sha=sha, **run_changes)
    return make_failure(run=run, job_id=job_id, status=status, signature_hash=f"{job_id:064x}")


def _success(run_id: int, attempt: int, sha: str, **run_changes: object) -> RunRef:
    return make_run_ref(run_id=run_id, run_attempt=attempt, head_sha=sha, **run_changes)


def _decide(success: RunRef, *failures: Failure) -> list[Resolution]:
    return decide(Outcome(success=success, completed_at=T0), list(failures))


def _ref(run_id: int, attempt: int, sha: str) -> SuccessRef:
    return SuccessRef(run_id=run_id, attempt=attempt, head_sha=sha)


# (name, failure, success) -> expected status or None. Failure at run 5000 attempt 1 on A.
CASES = [
    ("rerun of the same run: flaky", _failure(5000, 1, A), _success(5000, 2, A), FLAKY),
    ("attempt 3 after attempt 1: flaky", _failure(5000, 1, A), _success(5000, 3, A), FLAKY),
    ("another run, another sha: fixed by commit", _failure(5000, 1, A), _success(5001, 1, B), FIX),
    (
        "another run, another sha, success on attempt 2",
        _failure(5000, 2, A),
        _success(5001, 2, B),
        FIX,
    ),
    ("same sha in another run: nothing", _failure(5000, 1, A), _success(5001, 1, A), None),
    (
        "a failure of a later run: the branch is still red",
        _failure(5002, 1, B),
        _success(5001, 1, A),
        None,
    ),
    (
        "a later attempt of the same run than the success",
        _failure(5000, 2, A),
        _success(5000, 1, A),
        None,
    ),
    ("the same attempt is not later", _failure(5000, 1, A), _success(5000, 1, A), None),
    (
        "no branch on the success: nothing by commit",
        _failure(5000, 1, A, head_branch=None),
        _success(5001, 1, B, head_branch=None),
        None,
    ),
    (
        "no branch: a rerun of the same run is still flaky",
        _failure(5000, 1, A, head_branch=None),
        _success(5000, 2, A, head_branch=None),
        FLAKY,
    ),
    (
        "another workflow",
        _failure(5000, 1, A, workflow_id=8),
        _success(5001, 1, B),
        None,
    ),
    (
        "another fork with the same branch name",
        _failure(5000, 1, A, head_repo_full_name="alice/widgets", is_fork=True),
        _success(5001, 1, B, head_repo_full_name="bob/widgets", is_fork=True),
        None,
    ),
    (
        "a fork branch is not the base repository branch",
        _failure(5000, 1, A, head_repo_full_name="alice/widgets", is_fork=True),
        _success(5001, 1, B),
        None,
    ),
    ("another repository", _failure(5000, 1, A, repo_id=999), _success(5001, 1, B), None),
    (
        "already resolved by commit: unchanged",
        _failure(5000, 1, A, status=FIX),
        _success(5001, 1, B),
        None,
    ),
    (
        "already flaky: unchanged",
        _failure(5000, 1, A, status=FLAKY),
        _success(5000, 2, A),
        None,
    ),
    (
        "stale: unchanged",
        _failure(5000, 1, A, status=FailureStatus.STALE),
        _success(5001, 1, B),
        None,
    ),
]


@pytest.mark.parametrize(("name", "failure", "success", "expected"), CASES)
def test_decide(
    name: str, failure: Failure, success: RunRef, expected: FailureStatus | None
) -> None:
    resolutions = _decide(success, failure)

    if expected is None:
        assert resolutions == []
        return
    fix_sha = success.head_sha if expected is FIX else None
    assert resolutions == [
        Resolution(
            failure_id=failure.id,
            status=expected,
            fix_commit_sha=fix_sha,
            resolved_by=ResolvedBy(run_id=success.run_id, attempt=success.run_attempt),
        )
    ]


@pytest.mark.parametrize(("name", "failure", "success", "expected"), CASES)
def test_decide_late_agrees_with_decide(
    name: str, failure: Failure, success: RunRef, expected: FailureStatus | None
) -> None:
    """The late path reads the same rule from the failure, for the successes of its branch."""
    if (success.repo_id, success.workflow_id, success.branch_key) != (
        failure.job.job.run.repo_id,
        failure.job.job.run.workflow_id,
        failure.job.job.run.branch_key,
    ):
        return  # the store query never returns a success of another repo, workflow or branch
    late = decide_late(failure, [_ref(success.run_id, success.run_attempt, success.head_sha)])

    assert (late.status if late else None) == expected


def test_several_failures_of_one_run_get_the_same_fix_commit() -> None:
    failures = [_failure(5000, 1, A, job_id=n) for n in (1, 2, 3)]

    resolutions = _decide(_success(5001, 1, B), *failures)

    assert {r.failure_id for r in resolutions} == {f.id for f in failures}
    assert {(r.status, r.fix_commit_sha) for r in resolutions} == {(FIX, B)}


def test_only_the_failures_before_the_success_are_resolved() -> None:
    before = _failure(5000, 1, A, job_id=1)
    after = _failure(5003, 1, C, job_id=2)
    resolved = _failure(4999, 1, A, job_id=3, status=FIX)

    resolutions = _decide(_success(5001, 1, B), before, after, resolved)

    assert [r.failure_id for r in resolutions] == [before.id]


def test_decide_late_takes_the_earliest_success_that_resolves() -> None:
    failure = _failure(5000, 1, A)

    late = decide_late(
        failure,
        [_ref(5003, 1, C), _ref(5000, 2, A), _ref(5001, 1, B)],  # unsorted on purpose
    )

    assert late == Resolution(
        failure_id=failure.id,
        status=FLAKY,
        fix_commit_sha=None,
        resolved_by=ResolvedBy(run_id=5000, attempt=2),
    )


def test_decide_late_skips_a_success_with_the_same_sha_and_goes_on() -> None:
    failure = _failure(5000, 1, A)

    late = decide_late(failure, [_ref(5001, 1, A), _ref(5002, 1, B)])

    assert late == Resolution(
        failure_id=failure.id,
        status=FIX,
        fix_commit_sha=B,
        resolved_by=ResolvedBy(run_id=5002, attempt=1),
    )


def test_decide_late_ignores_successes_that_are_not_later() -> None:
    failure = _failure(5005, 1, A)

    assert decide_late(failure, [_ref(5000, 1, B), _ref(5005, 1, B)]) is None


def test_decide_late_without_successes_or_for_a_resolved_failure() -> None:
    assert decide_late(_failure(), []) is None
    assert decide_late(_failure(status=FLAKY), [_ref(5001, 1, B)]) is None


def test_decide_late_with_an_empty_branch_resolves_nothing_by_commit() -> None:
    failure = _failure(5000, 1, A, head_branch=None)

    assert decide_late(failure, [_ref(5001, 1, B)]) is None


# --- stale --------------------------------------------------------------------------------


def test_is_stale_at_the_edge_of_thirty_days() -> None:
    created = T0

    assert not is_stale(created, created + timedelta(days=30))
    assert is_stale(created, created + timedelta(days=30, seconds=1))
    assert not is_stale(created, created)
    assert is_stale(created, created + timedelta(days=2), after=timedelta(days=1))


# --- the order of arrival does not matter -------------------------------------------------

_RUN_IDS = [5000, 5001, 5002, 5003]


@st.composite
def _histories(draw: st.DrawFn) -> tuple[list[tuple[str, int, int]], dict[int, str]]:
    """Attempts of a few runs of one branch: each is a failure or a success, and every run has
    one head sha for all its attempts."""
    shas = {run_id: draw(st.sampled_from([A, B, C])) for run_id in _RUN_IDS}
    keys = [(run_id, attempt) for run_id in _RUN_IDS for attempt in (1, 2)]
    chosen = draw(st.lists(st.sampled_from(keys), min_size=1, max_size=8, unique=True))
    kinds = [draw(st.sampled_from(["failure", "success"])) for _ in chosen]
    events = [
        (kind, run_id, attempt) for kind, (run_id, attempt) in zip(kinds, chosen, strict=True)
    ]
    return events, shas


def _replay(events: list[tuple[str, int, int]], shas: dict[int, str]) -> dict[int, FailureStatus]:
    """Process the events in the given order the way D-83 does: a success is recorded and then
    resolves the open failures; a failure is saved and then looks for later recorded successes."""
    failures: dict[int, Failure] = {}
    successes: dict[tuple[int, int], RunRef] = {}
    for position, (kind, run_id, attempt) in enumerate(events):
        if kind == "success":
            success = _success(run_id, attempt, shas[run_id])
            successes[(run_id, attempt)] = success
            open_now = [f for f in failures.values() if f.status is FailureStatus.OPEN]
            for resolution in _decide(success, *open_now):
                failures[_index(failures, resolution)] = _apply(
                    failures[_index(failures, resolution)], resolution
                )
        else:
            failure = _failure(run_id, attempt, shas[run_id], job_id=position + 1)
            later = [
                _ref(s.run_id, s.run_attempt, s.head_sha)
                for s in successes.values()
                if (s.run_id, s.run_attempt) > (run_id, attempt)
            ]
            late = decide_late(failure, later)
            failures[position] = _apply(failure, late) if late else failure
    return {position: failure.status for position, failure in failures.items()}


def _index(failures: dict[int, Failure], resolution: Resolution) -> int:
    return next(key for key, f in failures.items() if f.id == resolution.failure_id)


def _apply(failure: Failure, resolution: Resolution) -> Failure:
    return failure.model_copy(
        update={"status": resolution.status, "fix_commit_sha": resolution.fix_commit_sha}
    )


@st.composite
def _two_orders(
    draw: st.DrawFn,
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]], dict[int, str]]:
    events, shas = draw(_histories())
    return events, draw(st.permutations(events)), shas


def _by_attempt(
    order: list[tuple[str, int, int]], shas: dict[int, str]
) -> dict[tuple[int, int], FailureStatus]:
    statuses = _replay(order, shas)
    positions = [i for i, (kind, _, _) in enumerate(order) if kind == "failure"]
    return {(order[pos][1], order[pos][2]): statuses[pos] for pos in positions}


@given(_two_orders())
def test_whether_a_failure_is_resolved_does_not_depend_on_the_arrival_order(
    case: tuple[list[tuple[str, int, int]], list[tuple[str, int, int]], dict[int, str]],
) -> None:
    events, shuffled, shas = case
    first, second = _by_attempt(events, shas), _by_attempt(shuffled, shas)
    successes = [
        _ref(run_id, attempt, shas[run_id]) for kind, run_id, attempt in events if kind == "success"
    ]

    for key, status in first.items():
        eligible = decide_late(_failure(key[0], key[1], shas[key[0]]), successes)
        # Resolved exactly when some success resolves it, in both orders.
        assert (status is FailureStatus.OPEN) == (eligible is None)
        assert (second[key] is FailureStatus.OPEN) == (eligible is None)
