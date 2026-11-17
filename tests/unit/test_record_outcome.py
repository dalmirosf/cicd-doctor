"""The success path on in-memory fakes (P2 e2a, D-83, D-84): a success is stored and resolves the
open failures it fixes, and a failure that arrives after its success is resolved at once.

The comment and the check run do not change yet (e2b): the tests only look at the store, and at
the fact that a success makes no call to GitHub.
"""

from typing import Any

import pytest

from cicd_doctor.core.models import FailureStatus, RecordOutcomePayload, RunRef
from cicd_doctor.core.usecases.record_run_outcome import RecordRunOutcome
from tests.factories import T0, make_run_ref
from tests.unit.test_diagnose_failed_run import World

A, B = "a" * 40, "b" * 40
FLAKY = FailureStatus.RESOLVED_FLAKY
FIX = FailureStatus.RESOLVED_BY_COMMIT
OPEN = FailureStatus.OPEN


def success_run(run_id: int = 5001, attempt: int = 1, sha: str = B, **changes: Any) -> RunRef:
    return make_run_ref(run_id=run_id, run_attempt=attempt, head_sha=sha, **changes)


def outcome_payload(run: RunRef, **changes: Any) -> RecordOutcomePayload:
    values: dict[str, Any] = {
        "run": run,
        "completed_at": T0,
        "default_branch": "main",
        "private": False,
        "account_login": "acme",
        "account_type": "Organization",
    }
    return RecordOutcomePayload(**{**values, **changes})


def use_case(world: World) -> RecordRunOutcome:
    return RecordRunOutcome(store=world.store, installations=world.installations)


async def record_success(world: World, run: RunRef, **changes: Any) -> None:
    await use_case(world).run(outcome_payload(run, **changes))


async def failure_of(world: World, run: RunRef | None = None) -> Any:
    [item] = (await world.record(run)).items
    return item.failure


async def test_a_rerun_without_code_changes_resolves_the_failure_as_flaky() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run(run_id=5000, attempt=2, sha=A))

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FLAKY, None)
    [found] = await world.store.resolved_by(world.run.repo_id, 5000, 2)
    assert found.id == failure.id


async def test_a_success_on_another_commit_resolves_the_failure_by_commit() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run())

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    assert [f.id for f in await world.store.resolved_by(world.run.repo_id, 5001, 1)] == [failure.id]


@pytest.mark.parametrize(
    ("name", "success"),
    [
        ("another branch", success_run(head_branch="other")),
        (
            "a fork with the same branch name",
            success_run(is_fork=True, head_repo_full_name="mallory/widgets"),
        ),
        ("another workflow", success_run(workflow_id=8)),
        ("another run on the same commit", success_run(sha=A)),
        ("a run with no branch", success_run(head_branch=None)),
        ("a run older than the failure", success_run(run_id=4999)),
    ],
)
async def test_a_success_that_does_not_fix_it_leaves_the_failure_open(
    name: str, success: RunRef
) -> None:
    world = World()
    await world.run_once()

    await record_success(world, success)

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (OPEN, None)
    assert await world.store.resolved_by(world.run.repo_id, success.run_id, 1) == []


async def test_a_success_in_another_repository_never_resolves_this_one() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run(repo_id=200, repo_full_name="acme/other"))

    assert (await failure_of(world)).status is OPEN


async def test_a_success_with_no_open_failures_makes_no_call_to_github() -> None:
    world = World()
    calls_before = list(world.journal)

    await record_success(world, success_run())

    assert world.journal == calls_before == []
    assert world.reporter.checks == {}
    assert world.reporter.comments == {}


async def test_resolving_a_failure_does_not_touch_the_check_run_or_the_comment() -> None:
    world = World()
    await world.run_once()
    journal = list(world.journal)
    [check] = world.reporter.checks.values()
    before = check.output

    await record_success(world, success_run())

    assert world.journal == journal
    assert check.output == before


async def test_a_retry_changes_nothing() -> None:
    world = World()
    await world.run_once()
    await record_success(world, success_run())
    first = await failure_of(world)

    await record_success(world, success_run())

    assert await failure_of(world) == first
    assert len(await world.store.resolved_by(world.run.repo_id, 5001, 1)) == 1


async def test_record_success_does_not_erase_the_pull_request_a_diagnosis_found() -> None:
    world = World()
    await world.run_once()
    assert (await world.record()).run.pr_number == 12

    # The webhook of a fork run has no pull request (D-72): its success must not erase it.
    await record_success(world, success_run(run_id=5000, attempt=2, sha=A, pr_number=None))

    assert (await world.record()).run.pr_number == 12


async def test_the_success_creates_the_installation_and_the_repository() -> None:
    world = World(jobs=False)

    await record_success(world, success_run(), private=True)

    installation = await world.installations.installation(1)
    assert installation is not None
    assert installation.account_login == "acme"


# --- a failure that arrives after its success (D-83) -----------------------------------------


async def test_a_late_failure_is_resolved_by_the_success_that_came_first() -> None:
    world = World()
    await record_success(world, success_run())  # the success of run 5001 arrives first

    await world.run_once()  # the diagnosis of run 5000 arrives after

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    assert [f.id for f in await world.store.resolved_by(world.run.repo_id, 5001, 1)] == [failure.id]


async def test_a_late_failure_of_an_earlier_attempt_is_flaky() -> None:
    world = World()
    await record_success(world, success_run(run_id=5000, attempt=2, sha=A))

    await world.run_once()

    assert (await failure_of(world)).status is FLAKY


async def test_a_late_failure_is_not_resolved_by_a_success_of_the_same_commit() -> None:
    world = World()
    await record_success(world, success_run(sha=A))

    await world.run_once()

    assert (await failure_of(world)).status is OPEN


async def test_a_failure_newer_than_the_success_stays_open() -> None:
    world = World()
    await record_success(world, success_run(run_id=4000))

    await world.run_once()

    assert (await failure_of(world)).status is OPEN


async def test_the_late_resolution_reads_the_successes_once_per_attempt() -> None:
    world = World()
    world.add_failed_job(world.run, 12)
    reads: list[tuple[Any, ...]] = []
    original = world.store.later_successes

    async def spy(*args: Any) -> Any:
        reads.append(args)
        return await original(*args)

    world.store.later_successes = spy  # type: ignore[method-assign,assignment]

    await world.run_once()

    assert len(reads) == 1


@pytest.mark.parametrize("success_first", [True, False])
async def test_the_final_state_does_not_depend_on_the_order(success_first: bool) -> None:
    world = World()
    success = success_run()

    if success_first:
        await record_success(world, success)
        await world.run_once()
    else:
        await world.run_once()
        await record_success(world, success)

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    resolved = await world.store.resolved_by(world.run.repo_id, 5001, 1)
    assert [f.id for f in resolved] == [failure.id]
