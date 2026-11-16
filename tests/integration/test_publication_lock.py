"""The publication lock of the Postgres store across "processes" (SPEC §12, D-69, D-70).

Each ``PostgresFailureStore.connect`` opens an engine of its own, so two stores stand for two
workers: the only thing between them is a session-level advisory lock in the database. The
generic behaviour (waiting, the transient error, release on failure) is in the contract suite;
here are the parts that only a real server shows: a connection that dies, a waiter that is
cancelled, and what the lock leaves behind in the pool.
"""

import asyncio
import time
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import PublicationKey
from tests.factories import T0
from tests.pg import libpq_url

KEY = PublicationKey.comment(repo_id=100, pr_number=12, workflow_id=7)
OTHER_KEY = PublicationKey.check(repo_id=100, run_id=5000)


def _rows(url: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        return conn.execute(query).fetchall()


def _holders(url: str) -> list[int]:
    """Backend pids that hold an advisory lock of this database."""
    rows = _rows(
        url,
        """
        SELECT pid FROM pg_locks
        WHERE locktype = 'advisory' AND granted
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
        """,
    )
    return [pid for (pid,) in rows]


async def _take(store: PostgresFailureStore, key: PublicationKey = KEY) -> float:
    """Hold the lock for an instant and say when it was given."""
    async with store.publication_lock(key):
        return time.monotonic()


async def test_the_second_owner_gets_the_lock_when_the_first_releases_it(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with (
        PostgresFailureStore.connect(pg_clean, clock) as first,
        PostgresFailureStore.connect(pg_clean, clock) as second,
    ):
        async with first.publication_lock(KEY):
            waiting = asyncio.create_task(_take(second))
            await asyncio.sleep(0.3)
            assert not waiting.done()  # blocked inside the database, not polling and giving up
            released_at = time.monotonic()
        acquired_at = await asyncio.wait_for(waiting, timeout=5)

    assert acquired_at >= released_at
    assert _holders(pg_clean) == []  # and nothing stays locked when both are done


async def test_the_lock_is_held_on_one_pooled_connection_for_the_whole_critical_section(
    pg_clean: str,
) -> None:
    async with PostgresFailureStore.connect(pg_clean, FakeClock(T0)) as store:
        assert _holders(pg_clean) == []
        async with store.publication_lock(KEY):
            holders = _holders(pg_clean)
            # Other work of the same store (a save, a read) uses other connections of the pool.
            assert await store.publication(KEY) is None
            assert _holders(pg_clean) == holders
        assert len(holders) == 1
        assert _holders(pg_clean) == []


async def test_the_lock_passes_to_a_waiter_when_the_owner_connection_dies(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with (
        PostgresFailureStore.connect(pg_clean, clock) as first,
        PostgresFailureStore.connect(pg_clean, clock) as second,
    ):
        async with first.publication_lock(KEY):
            [owner_pid] = _holders(pg_clean)
            waiting = asyncio.create_task(_take(second))
            await asyncio.sleep(0.3)
            assert not waiting.done()

            _rows(pg_clean, f"SELECT pg_terminate_backend({owner_pid})")  # a crash, a failover

            await asyncio.wait_for(waiting, timeout=5)
        # Leaving the critical section of the dead owner raised nothing, and its pool recovered.
        async with first.publication_lock(OTHER_KEY):
            pass
    assert _holders(pg_clean) == []


async def test_a_waiter_past_the_wait_gets_a_transient_error_and_leaks_nothing(
    pg_clean: str,
) -> None:
    clock = FakeClock(T0)
    async with (
        PostgresFailureStore.connect(pg_clean, clock) as first,
        PostgresFailureStore.connect(pg_clean, clock, lock_wait=timedelta(seconds=1)) as second,
    ):
        async with first.publication_lock(KEY):
            started = time.monotonic()
            with pytest.raises(TransientError, match="busy"):
                await _take(second)
            waited = time.monotonic() - started
        assert 0.8 <= waited < 5  # it waited its second, no more
        assert _holders(pg_clean) == []
        assert await _take(second) > 0  # the same store can take it once it is free


async def test_a_cancelled_waiter_leaves_no_session_waiting_for_the_lock(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with (
        PostgresFailureStore.connect(pg_clean, clock) as first,
        PostgresFailureStore.connect(pg_clean, clock) as second,
        PostgresFailureStore.connect(pg_clean, clock) as third,
    ):
        async with first.publication_lock(KEY):
            abandoned = asyncio.create_task(_take(second))
            await asyncio.sleep(0.3)
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
        # A statement cut short must not stay queued in the server and grab the lock later:
        # the next owner gets it at once and is the only one that holds it.
        async with third.publication_lock(KEY):
            assert len(_holders(pg_clean)) == 1
            waiting = _rows(
                pg_clean,
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted",
            )
            assert waiting == [(0,)]
    assert _holders(pg_clean) == []


async def test_the_lock_timeout_does_not_leak_into_the_pool(pg_clean: str) -> None:
    async with PostgresFailureStore.connect(pg_clean, FakeClock(T0), pool_size=1) as store:
        async with store.publication_lock(KEY):
            pass
        async with store._engine.connect() as conn:  # the one pooled connection, reused
            shown = (await conn.execute(text("SHOW lock_timeout"))).scalar()

    assert shown == "0"  # an unrelated row lock must never inherit the publication wait
