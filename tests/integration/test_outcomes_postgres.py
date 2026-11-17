"""The success path on Postgres (P2 e2a, D-83): signed-webhook jobs through the real queue and
store. A failure and its success are processed in both orders, and by two consumers at once: in
every case the failure ends resolved by the success, once."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import psycopg
import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from tests.diagnose_world import DiagnoseWorld, diagnose_world
from tests.pg import libpq_url

FIX_SHA = "b" * 40
Row = tuple[str, str | None, int | None, int | None]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))


@pytest.fixture
async def world(pg_clean: str, clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock, queue=queue, store=store, ledger=ledger, installations=installations
        ) as built,
    ):
        yield built


def failures(url: str) -> list[Row]:
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        return conn.execute(
            "SELECT status, fix_commit_sha, resolved_by_run_id, resolved_by_attempt FROM failures"
        ).fetchall()


RESOLVED: Row = ("resolved_by_commit", FIX_SHA, 5001, 1)


@pytest.mark.parametrize("success_first", [False, True])
async def test_the_failure_ends_resolved_in_either_order(
    world: DiagnoseWorld, pg_clean: str, success_first: bool
) -> None:
    world.add_run(5000)
    if success_first:
        await world.enqueue_success()
        assert await world.worker.run_one("a")
        await world.enqueue(5000)
        assert await world.worker.run_one("a")
    else:
        await world.enqueue(5000)
        assert await world.worker.run_one("a")
        assert failures(pg_clean)[0][0] == "open"
        await world.enqueue_success()
        assert await world.worker.run_one("a")

    assert failures(pg_clean) == [RESOLVED]
    assert len(world.llm.calls) == 1


async def test_two_consumers_processing_the_failure_and_its_success_at_once(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    world.add_run(5000)
    await world.enqueue(5000)
    await world.enqueue_success()

    first, second = await asyncio.gather(world.worker.run_one("a"), world.worker.run_one("b"))

    assert (first, second) == (True, True)
    assert failures(pg_clean) == [RESOLVED]
    with psycopg.connect(libpq_url(pg_clean), connect_timeout=5) as conn:
        [(attempts,)] = conn.execute(
            "SELECT count(*) FROM run_attempts WHERE conclusion = 'success'"
        ).fetchall()
    assert attempts == 1
