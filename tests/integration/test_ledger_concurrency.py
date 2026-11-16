"""The budget holds under concurrency (SPEC §12, D-69).

Two engines stand for two worker processes: each has its own pool, so the only thing that
serialises their reservations is the advisory lock inside the database, as in production. Forty
reservations race for a budget (or a call cap) that allows fewer of them.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.core.models import Reservation
from tests.factories import T0
from tests.pg import scalar

RACERS = 40
ESTIMATE = Decimal("0.10")
DAY = T0.date()


@pytest.fixture
async def engines(pg_clean: str) -> AsyncIterator[list[AsyncEngine]]:
    created = [make_engine(pg_clean, pool_size=10), make_engine(pg_clean, pool_size=10)]
    yield created
    for engine in created:
        await engine.dispose()


def _ledgers(
    engines: list[AsyncEngine], *, calls: int, budget: Decimal, clock: FakeClock | None = None
) -> list[PostgresUsageLedger]:
    clock = clock or FakeClock(T0)
    return [
        PostgresUsageLedger(engine, clock, calls_per_installation=calls, daily_budget_usd=budget)
        for engine in engines
    ]


async def _race(ledgers: list[PostgresUsageLedger], installations: int) -> list[Reservation | None]:
    """``RACERS`` reservations at once, alternating between the two processes."""
    return await asyncio.gather(
        *(ledgers[i % 2].reserve(i % installations + 1, ESTIMATE) for i in range(RACERS))
    )


async def test_a_budget_for_seven_calls_grants_exactly_seven(
    pg_clean: str, engines: list[AsyncEngine]
) -> None:
    budget = 7 * ESTIMATE
    ledgers = _ledgers(engines, calls=100, budget=budget)

    results = await _race(ledgers, installations=4)

    assert sum(result is not None for result in results) == 7
    assert scalar(pg_clean, "SELECT SUM(calls) FROM llm_usage") == 7
    held = scalar(pg_clean, "SELECT SUM(reserved_usd + spent_usd) FROM llm_usage")
    assert held == budget  # all of it, and never more


async def test_a_cap_of_thirty_calls_grants_exactly_thirty(
    pg_clean: str, engines: list[AsyncEngine]
) -> None:
    ledgers = _ledgers(engines, calls=30, budget=Decimal(1000))

    results = await _race(ledgers, installations=1)

    assert sum(result is not None for result in results) == 30
    row = await ledgers[1].usage(DAY, 1)  # read through the other "process"
    assert (row.calls, row.reserved_usd) == (30, 30 * ESTIMATE)


async def test_concurrent_settles_lose_no_amount(pg_clean: str, engines: list[AsyncEngine]) -> None:
    ledgers = _ledgers(engines, calls=100, budget=Decimal(1000))
    granted = [r for r in await _race(ledgers, installations=2) if r is not None]
    assert len(granted) == RACERS
    actual = [Decimal(i + 1) / 1000 for i in range(RACERS)]  # 0.001 .. 0.040, all different

    await asyncio.gather(*(ledgers[i % 2].settle(r, actual[i]) for i, r in enumerate(granted)))

    rows = [await ledgers[0].usage(DAY, installation) for installation in (1, 2)]
    assert sum(row.spent_usd for row in rows) == sum(actual)
    assert sum(row.reserved_usd for row in rows) == Decimal(0)
    assert sum(row.calls for row in rows) == RACERS


async def test_money_freed_by_a_settle_is_reusable_and_never_duplicated(
    pg_clean: str, engines: list[AsyncEngine]
) -> None:
    budget = 3 * ESTIMATE
    ledgers = _ledgers(engines, calls=100, budget=budget)
    holds = [await ledgers[0].reserve(1, ESTIMATE) for _ in range(3)]
    assert all(hold is not None for hold in holds)
    assert await ledgers[1].reserve(1, ESTIMATE) is None  # the budget is full
    free_a, free_b = holds[0], holds[1]
    assert free_a is not None
    assert free_b is not None

    # Two holds settle for nothing while three new calls ask for the money, in any order.
    *_, r1, r2, r3 = await asyncio.gather(
        ledgers[0].settle(free_a, Decimal(0)),
        ledgers[1].settle(free_b, Decimal(0)),
        ledgers[0].reserve(1, ESTIMATE),
        ledgers[1].reserve(1, ESTIMATE),
        ledgers[0].reserve(1, ESTIMATE),
    )
    granted = [r for r in (r1, r2, r3) if r is not None]
    while (extra := await ledgers[1].reserve(1, ESTIMATE)) is not None:  # take what is left
        granted.append(extra)

    assert len(granted) == 2  # exactly the two freed holds: nothing leaked, nothing invented
    held = scalar(pg_clean, "SELECT SUM(reserved_usd + spent_usd) FROM llm_usage")
    assert held == budget


async def test_the_day_comes_from_the_clock_not_the_database(
    pg_clean: str, engines: list[AsyncEngine]
) -> None:
    clock = FakeClock(T0)
    [ledger, _] = _ledgers(engines, calls=1, budget=Decimal(1000), clock=clock)
    assert await ledger.reserve(1, ESTIMATE) is not None
    assert await ledger.reserve(1, ESTIMATE) is None

    clock.advance(timedelta(days=1))

    assert await ledger.reserve(1, ESTIMATE) is not None
    assert scalar(pg_clean, "SELECT count(*) FROM llm_usage") == 2
