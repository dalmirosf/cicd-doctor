"""The whole command on Postgres (P3 d2, D-96): the queue, the failure store, the usage ledger and
the command log are real; GitHub is ``FakeGitHub`` behind the real adapters and the LLM is
``FakeLLM``. Two workers drain the same queue at the same time, as in production."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import psycopg
import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.adapters.postgres.command_log import PostgresCommandLog
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from tests.diagnose_world import DiagnoseWorld, diagnose_world
from tests.e2e.test_diagnose_e2e import CHECK, COMMENT
from tests.e2e.test_explain_runs_e2e import (
    add_fork_pull,
    diagnose_automatically,
    enqueue_command,
    fork_runs,
)
from tests.pg import libpq_url


def rows(url: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        return conn.execute(query).fetchall()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.fromisoformat("2026-11-20T12:00:00+00:00"))


@pytest.fixture
async def world(pg_clean: str, clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock, pool_size=8) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        PostgresCommandLog.connect(pg_clean) as commands,
        diagnose_world(
            clock=clock,
            queue=queue,
            store=store,
            ledger=ledger,
            installations=installations,
            llm_script=[valid()] * 9,
            commands=commands,
        ) as built,
    ):
        yield built


async def drain_with_two_workers(world: DiagnoseWorld) -> None:
    """Two consumers over one queue until nothing is ready: the children run side by side."""

    async def consume(name: str) -> None:
        while await world.worker.run_one(name):
            pass

    await asyncio.gather(consume("a"), consume("b"))


async def test_a_command_over_postgres_upgrades_each_run_once(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    add_fork_pull(world)
    fork_runs(world, {5001: 1, 5002: 2, 5003: 3})
    for run_id, workflow_id in ((5001, 1), (5002, 2), (5003, 3)):
        await diagnose_automatically(world, run_id, workflow_id)
    assert world.llm.calls == []

    await enqueue_command(world, 777)
    await drain_with_two_workers(world)

    assert world.writes.reactions == [(777, "eyes")]
    assert len(world.llm.calls) == 3
    assert rows(pg_clean, "SELECT count(*) FROM command_invocations") == [(1,)]
    assert (
        rows(
            pg_clean, "SELECT kind, status FROM jobs WHERE dedupe_key LIKE 'explain:%' ORDER BY id"
        )
        == [("explain", "done")] + [("diagnose_run", "done")] * 3
    )
    assert rows(
        pg_clean,
        "SELECT run_id, source, published FROM diagnoses d JOIN failures f ON f.id = d.failure_id "
        "ORDER BY run_id, published",
    ) == [
        (run, source, published)
        for run in (5001, 5002, 5003)
        for source, published in (("rules", False), ("llm", True))
    ]
    assert rows(pg_clean, "SELECT count(*) FROM check_publications") == [(3,)]
    assert len(world.requests("PATCH", CHECK)) == 6  # 3 automatic + 3 upgrades, none created
    assert len(world.requests("PATCH", COMMENT)) == 3
    assert [r[0] for r in rows(pg_clean, "SELECT status FROM jobs WHERE status <> 'done'")] == []


async def test_a_child_that_dies_does_not_affect_the_others(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    add_fork_pull(world)
    fork_runs(world, {5001: 1, 5002: 2})
    await diagnose_automatically(world, 5001, 1)
    # Run 5002 was never diagnosed and GitHub has no jobs for it: its child fails for good.
    await enqueue_command(world, 777)
    first = await world.worker.run_one("a")  # the command job itself
    assert first is True

    await drain_with_two_workers(world)

    statuses = dict(
        rows(
            pg_clean,
            "SELECT dedupe_key, status FROM jobs WHERE kind = 'diagnose_run' "
            "AND dedupe_key LIKE 'explain:%'",
        )
    )
    assert statuses["explain:777:5001:1"] == "done"
    assert statuses["explain:777:5002:1"] in {"failed", "dead"}
    assert len(world.llm.calls) == 1  # the healthy run was upgraded
    assert world.writes.reactions == [(777, "eyes")]
