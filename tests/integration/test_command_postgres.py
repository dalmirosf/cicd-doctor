"""The command limit on the real database (FR-19, D-93).

The behaviour every adapter shares is in ``tests/contract/test_command_log.py``. Here are the
database facts: two engines (as two workers) racing for one PR give exactly one winner, many claims
over a few PRs leave one row per PR, and the retention purge works in batches.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from cicd_doctor.adapters.postgres.command_log import PostgresCommandLog
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.core.usecases.purge import PurgeConfig
from tests.factories import T0
from tests.pg import scalar

WINDOW = timedelta(minutes=5)
RACERS = 40
PULLS = 4


@pytest.fixture
async def logs(pg_clean: str) -> AsyncIterator[list[PostgresCommandLog]]:
    """Two logs over two engines: only the advisory lock serialises them."""
    engines: list[AsyncEngine] = [
        make_engine(pg_clean, pool_size=10),
        make_engine(pg_clean, pool_size=10),
    ]
    yield [PostgresCommandLog(engine, purge_batch=3) for engine in engines]
    for engine in engines:
        await engine.dispose()


async def test_two_engines_claiming_the_same_pull_request_give_exactly_one_winner(
    logs: list[PostgresCommandLog], pg_clean: str
) -> None:
    results = await asyncio.gather(
        logs[0].claim(100, 12, 1, "ann", T0, WINDOW), logs[1].claim(100, 12, 2, "bob", T0, WINDOW)
    )

    assert sorted(results) == [False, True]
    assert scalar(pg_clean, "SELECT count(*) FROM command_invocations") == 1


async def test_forty_concurrent_claims_over_four_pull_requests_leave_four_rows(
    logs: list[PostgresCommandLog], pg_clean: str
) -> None:
    results = await asyncio.gather(
        *(
            logs[n % 2].claim(100, n % PULLS + 1, 1000 + n, f"user{n}", T0, WINDOW)
            for n in range(RACERS)
        )
    )

    assert results.count(True) == PULLS
    assert scalar(pg_clean, "SELECT count(*) FROM command_invocations") == PULLS
    assert scalar(pg_clean, "SELECT count(DISTINCT pr_number) FROM command_invocations") == PULLS


async def test_the_same_comment_racing_with_itself_is_accepted_both_times_with_one_row(
    logs: list[PostgresCommandLog], pg_clean: str
) -> None:
    results = await asyncio.gather(
        logs[0].claim(100, 12, 1, "ann", T0, WINDOW), logs[1].claim(100, 12, 1, "ann", T0, WINDOW)
    )

    assert list(results) == [True, True]
    assert scalar(pg_clean, "SELECT count(*) FROM command_invocations") == 1


async def test_the_stored_time_is_the_one_the_caller_gives_not_the_one_of_the_database(
    logs: list[PostgresCommandLog], pg_clean: str
) -> None:
    await logs[0].claim(100, 12, 1, "ann", T0, WINDOW)

    assert scalar(pg_clean, "SELECT created_at FROM command_invocations") == T0


async def test_the_purge_deletes_old_rows_in_batches_and_keeps_the_new_ones(
    logs: list[PostgresCommandLog], pg_clean: str
) -> None:
    for pr in range(1, 8):  # 7 old rows: three batches of 3
        await logs[0].claim(100, pr, pr, "ann", T0 - timedelta(days=31), WINDOW)
    for pr in range(10, 12):
        await logs[0].claim(100, pr, pr, "ann", T0 - timedelta(days=29), WINDOW)

    counts = await logs[1].purge(T0 - timedelta(days=PurgeConfig().commands_days))

    assert counts.deleted == {"command_invocations": 7}
    assert scalar(pg_clean, "SELECT count(*) FROM command_invocations") == 2
