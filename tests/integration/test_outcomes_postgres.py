"""The success path on Postgres (P2 e2a and e2b, D-83, D-84): jobs through the real queue and
store. A failure and its success are processed in both orders, and by two consumers at once: in
every case the failure ends resolved by the success, once, and the PR has exactly one comment,
which shows the resolved state."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

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
COMMENTS = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
COMMENT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"
BANNER = "**✅ Fixed by `bbbbbbb`**"
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


def assert_one_resolved_comment(world: DiagnoseWorld, *, edited: bool) -> None:
    """One comment was ever created. It shows the banner, written either at creation (the
    success came first) or by an edit of that same comment."""
    [posted] = world.requests("POST", COMMENTS)
    [comment] = world.writes.comments.values()
    assert BANNER in comment["body"]
    assert "Likely cause" not in comment["body"]
    assert (BANNER in posted.json["body"]) is not edited
    assert len(world.requests("PATCH", COMMENT)) == (1 if edited else 0)


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
    assert_one_resolved_comment(world, edited=not success_first)


async def test_two_consumers_processing_the_failure_and_its_success_at_once(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    world.add_run(5000)
    await world.enqueue(5000)
    await world.enqueue_success()

    first, second = await asyncio.gather(world.worker.run_one("a"), world.worker.run_one("b"))

    assert (first, second) == (True, True)
    assert failures(pg_clean) == [RESOLVED]
    assert len(world.requests("POST", COMMENTS)) == 1
    [comment] = world.writes.comments.values()
    assert BANNER in comment["body"]
    with psycopg.connect(libpq_url(pg_clean), connect_timeout=5) as conn:
        [(attempts,)] = conn.execute(
            "SELECT count(*) FROM run_attempts WHERE conclusion = 'success'"
        ).fetchall()
    assert attempts == 1


async def test_a_success_recorded_while_the_diagnosis_is_between_its_reads_is_not_lost(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    """The forced interleaving: the diagnosis reads the successes and finds none, then waits
    until the other consumer has recorded the success and resolved the failure. The success
    finds no comment to rewrite, so the diagnosis must write the resolved state itself."""
    world.add_run(5000)
    await world.enqueue(5000)
    await world.enqueue_success()
    gate = asyncio.Event()
    original = world.store.later_successes

    async def held(*args: Any) -> Any:
        found = await original(*args)
        await gate.wait()
        return found  # read before the success existed

    world.store.later_successes = held  # type: ignore[method-assign,assignment]

    async def consume(name: str) -> bool:
        done = await world.worker.run_one(name)
        gate.set()  # only the consumer that is not held can finish first
        return done

    first, second = await asyncio.gather(consume("a"), consume("b"))

    assert (first, second) == (True, True)
    assert failures(pg_clean) == [RESOLVED]
    assert_one_resolved_comment(world, edited=False)
