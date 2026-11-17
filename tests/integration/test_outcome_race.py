"""A failure and the success that comes after it, written by two workers (SPEC §12, D-83).

Two operations of each side decide the outcome: the failure side *writes* the failure (the
checkpoint) and then *looks* for a later success; the success side *writes* the success and then
*looks* for open failures. Each worker has its own engine, so the two sides really are two
connections. For every interleaving of the four operations (the order inside each side is fixed),
the failure must end resolved: either the success finds the failure, or the failure finds the
success, or both do and the second ``resolve`` changes nothing.
"""

import asyncio
from collections.abc import Awaitable, Callable
from itertools import combinations

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.models import (
    FailureStatus,
    Installation,
    Repository,
    ResolvedBy,
    RunRef,
)
from cicd_doctor.core.outcomes import Outcome, decide, decide_late
from tests.factories import (
    T0,
    make_run_analysis,
    make_run_attempt,
    make_run_ref,
    make_workflow_run,
)
from tests.pg import reset_tables

FAILED = make_run_ref(run_id=5000, run_attempt=1, head_sha="a" * 40)
SUCCEEDED = make_run_ref(run_id=5001, run_attempt=1, head_sha="b" * 40)
Step = Callable[[], Awaitable[None]]


async def _write_failure(store: PostgresFailureStore) -> None:
    await store.save_run_analysis(make_run_analysis(FAILED))


async def _look_for_a_later_success(store: PostgresFailureStore) -> None:
    """The diagnosis side (D-83): read the recorded successes, resolve the failure if one fits."""
    record = await store.run_analysis(FAILED.run_id, FAILED.run_attempt)
    assert record is not None
    successes = await store.later_successes(
        FAILED.repo_id, FAILED.workflow_id, FAILED.branch_key, FAILED.run_id, FAILED.run_attempt
    )
    for item in record.items:
        resolution = decide_late(item.failure, successes)
        if resolution is not None:
            await store.resolve(
                [resolution.failure_id],
                resolution.status,
                resolution.fix_commit_sha,
                ResolvedBy(run_id=SUCCEEDED.run_id, attempt=SUCCEEDED.run_attempt),
            )


async def _write_success(store: PostgresFailureStore) -> None:
    await store.record_success(
        make_workflow_run(SUCCEEDED), make_run_attempt(SUCCEEDED, conclusion="success")
    )


async def _look_for_open_failures(store: PostgresFailureStore) -> None:
    """The success side: read the open failures of the branch and resolve the ones that fit."""
    found = await store.open_failures(FAILED.repo_id, FAILED.workflow_id, FAILED.branch_key)
    outcome = Outcome(success=SUCCEEDED, completed_at=T0)
    for resolution in decide(outcome, found):
        await store.resolve(
            [resolution.failure_id],
            resolution.status,
            resolution.fix_commit_sha,
            ResolvedBy(run_id=SUCCEEDED.run_id, attempt=SUCCEEDED.run_attempt),
        )


async def _prepare(url: str) -> None:
    engine = make_engine(url)
    try:
        installations = PostgresInstallationStore(engine)
        await installations.save_installation(
            Installation(id=1, account_login="acme", account_type="Organization")
        )
        await installations.save_repositories(
            [
                Repository(
                    id=FAILED.repo_id,
                    installation_id=1,
                    full_name=FAILED.repo_full_name,
                    private=False,
                    default_branch="main",
                )
            ]
        )
    finally:
        await engine.dispose()


def _interleavings() -> list[tuple[str, ...]]:
    """Every order of F1 F2 S1 S2 that keeps F1 before F2 and S1 before S2: six of them."""
    orders = []
    for failure_slots in combinations(range(4), 2):
        order = ["S"] * 4
        for slot in failure_slots:
            order[slot] = "F"
        orders.append(tuple(order))
    return orders


async def _statuses(store: PostgresFailureStore, run: RunRef) -> list[FailureStatus]:
    record = await store.run_analysis(run.run_id, run.run_attempt)
    assert record is not None
    return [item.failure.status for item in record.items]


@pytest.mark.parametrize("order", _interleavings(), ids="".join)
async def test_the_failure_ends_resolved_in_every_interleaving(pg_url: str, order: str) -> None:
    reset_tables(pg_url)
    await _prepare(pg_url)
    clock = FakeClock(T0)
    async with (
        PostgresFailureStore.connect(pg_url, clock) as failure_side,
        PostgresFailureStore.connect(pg_url, clock) as success_side,
    ):
        failure_steps: list[Step] = [
            lambda: _write_failure(failure_side),
            lambda: _look_for_a_later_success(failure_side),
        ]
        success_steps: list[Step] = [
            lambda: _write_success(success_side),
            lambda: _look_for_open_failures(success_side),
        ]
        for side in order:
            await (failure_steps if side == "F" else success_steps).pop(0)()

        # One attempt of the failed run has failures; they all end resolved by commit.
        # (The failure side wrote its record first or the success side could not have seen it.)
        record = await failure_side.run_analysis(FAILED.run_id, FAILED.run_attempt)
        assert record is not None
        assert [item.failure.status for item in record.items] == [
            FailureStatus.RESOLVED_BY_COMMIT
        ] * len(record.items)
        assert {item.failure.fix_commit_sha for item in record.items} == {SUCCEEDED.head_sha}


async def test_the_failure_ends_resolved_when_both_sides_run_at_once(pg_url: str) -> None:
    clock = FakeClock(T0)
    for _ in range(15):
        reset_tables(pg_url)
        await _prepare(pg_url)
        async with (
            PostgresFailureStore.connect(pg_url, clock) as failure_side,
            PostgresFailureStore.connect(pg_url, clock) as success_side,
        ):

            async def failure_work(store: PostgresFailureStore = failure_side) -> None:
                await _write_failure(store)
                await _look_for_a_later_success(store)

            async def success_work(store: PostgresFailureStore = success_side) -> None:
                await _write_success(store)
                await _look_for_open_failures(store)

            await asyncio.gather(failure_work(), success_work())

            statuses = await _statuses(failure_side, FAILED)
            assert statuses == [FailureStatus.RESOLVED_BY_COMMIT] * len(statuses)
