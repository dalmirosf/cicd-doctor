"""Contract suite for the ``CommandLog`` port (FR-19, D-93).

Every adapter must pass these tests: the in-memory one and the Postgres one, whose parameter is
marked ``integration`` (D-51). The clock is the caller's: the log never reads the time itself.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryCommandLog
from cicd_doctor.adapters.postgres.command_log import PostgresCommandLog
from cicd_doctor.core.ports import CommandLog
from tests.factories import T0

WINDOW = timedelta(minutes=5)


@dataclass
class Harness:
    log: CommandLog
    clock: FakeClock
    count: Callable[[int | None], Awaitable[int]]

    async def claim(
        self, comment_id: int, *, repo: int = 100, pr: int = 12, actor: str = "ann"
    ) -> bool:
        return await self.log.claim(repo, pr, comment_id, actor, self.clock.now(), WINDOW)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    clock = FakeClock(T0)
    if request.param == "memory":
        memory = InMemoryCommandLog()

        async def memory_count(repo_id: int | None) -> int:
            return memory.count(repo_id)

        yield Harness(memory, clock, memory_count)
        return
    url: str = request.getfixturevalue("pg_clean")
    async with PostgresCommandLog.connect(url) as postgres:
        yield Harness(postgres, clock, postgres.count)


async def test_the_first_command_of_a_pull_request_is_accepted_and_recorded(
    harness: Harness,
) -> None:
    assert await harness.claim(1) is True

    assert await harness.count(None) == 1


async def test_a_second_command_at_4_59_is_refused_and_leaves_no_row(harness: Harness) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(minutes=4, seconds=59))

    assert await harness.claim(2) is False

    assert await harness.count(None) == 1


async def test_a_second_command_at_5_01_is_accepted(harness: Harness) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(minutes=5, seconds=1))

    assert await harness.claim(2) is True


async def test_exactly_at_the_window_the_old_claim_no_longer_counts(harness: Harness) -> None:
    await harness.claim(1)
    harness.clock.advance(WINDOW)

    assert await harness.claim(2) is True


async def test_the_window_is_counted_from_the_last_accepted_command(harness: Harness) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(minutes=3))
    assert await harness.claim(2) is False  # refused: extends nothing
    harness.clock.advance(timedelta(minutes=2, seconds=1))  # 5:01 after the first

    assert await harness.claim(3) is True
    harness.clock.advance(timedelta(minutes=4))
    assert await harness.claim(4) is False  # now it counts from the third


async def test_the_same_comment_claims_again_without_spending_a_new_window(
    harness: Harness,
) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(minutes=2))

    assert await harness.claim(1) is True  # a retried job or a redelivery

    assert await harness.count(None) == 1
    harness.clock.advance(timedelta(minutes=3, seconds=1))  # 5:01 after the first, not the retry
    assert await harness.claim(2) is True


async def test_a_retry_is_accepted_even_when_a_later_command_holds_the_window(
    harness: Harness,
) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(minutes=6))
    await harness.claim(2)

    assert await harness.claim(1) is True  # comment 1 has its row: still the same command


async def test_pull_requests_do_not_share_a_window(harness: Harness) -> None:
    assert await harness.claim(1, pr=12) is True
    assert await harness.claim(2, pr=13) is True
    assert await harness.claim(3, pr=12) is False


async def test_repositories_do_not_share_a_window_even_with_the_same_pr_number(
    harness: Harness,
) -> None:
    assert await harness.claim(1, repo=100, pr=12) is True
    assert await harness.claim(2, repo=200, pr=12) is True
    assert await harness.claim(3, repo=100, pr=12) is False
    assert await harness.count(100) == 1
    assert await harness.count(200) == 1


async def test_the_same_comment_id_in_two_repositories_is_two_commands(harness: Harness) -> None:
    assert await harness.claim(5, repo=100) is True
    assert await harness.claim(5, repo=200) is True

    assert await harness.count(None) == 2


async def test_a_stable_order_the_first_claim_wins_and_the_rest_lose(harness: Harness) -> None:
    results = [await harness.claim(comment) for comment in (10, 11, 12, 13)]

    assert results == [True, False, False, False]


async def test_concurrent_claims_for_one_pull_request_have_exactly_one_winner(
    harness: Harness,
) -> None:
    results = await asyncio.gather(*(harness.claim(1000 + n) for n in range(8)))

    assert results.count(True) == 1
    assert await harness.count(None) == 1


async def test_purge_deletes_only_the_claims_older_than_the_cut(harness: Harness) -> None:
    await harness.claim(1, pr=1)
    harness.clock.advance(timedelta(days=31))
    await harness.claim(2, pr=2)
    harness.clock.advance(timedelta(days=1))

    counts = await harness.log.purge(harness.clock.now() - timedelta(days=30))

    assert counts.deleted == {"command_invocations": 1}
    assert await harness.count(None) == 1
    assert await harness.claim(2, pr=2) is True  # its row is still there


async def test_purge_is_idempotent(harness: Harness) -> None:
    await harness.claim(1)
    harness.clock.advance(timedelta(days=40))
    cut = harness.clock.now() - timedelta(days=30)

    assert (await harness.log.purge(cut)).total == 1
    assert (await harness.log.purge(cut)).total == 0
