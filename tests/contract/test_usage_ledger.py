"""Contract suite for the ``UsageLedger`` port (FR-13, D-16, SPEC §6 ``llm_usage``, D-69).

Every adapter must pass these tests: the in-memory one and the Postgres one, whose parameter is
marked ``integration`` (D-51). The row of ``llm_usage`` is read through a small per-adapter helper
of the ``Harness``, because the port exposes only ``reserve`` and ``settle``.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory.ledger import InMemoryUsageLedger
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.core.models import Reservation
from cicd_doctor.core.ports import Clock, UsageLedger
from tests.factories import T0

CENT = Decimal("0.01")


class Row(Protocol):
    """What both adapters' ``usage`` rows have: read-only here."""

    @property
    def calls(self) -> int: ...

    @property
    def reserved_usd(self) -> Decimal: ...

    @property
    def spent_usd(self) -> Decimal: ...


@dataclass
class Harness:
    clock: FakeClock
    build: Callable[[Clock, dict[str, Any]], UsageLedger]
    read: Callable[[UsageLedger, date, int], Awaitable[Row]]
    current: UsageLedger | None = field(default=None, init=False)

    def ledger(self, *, clock: Clock | None = None, **options: Any) -> UsageLedger:
        """A ledger with the given limits; ``row`` reads what it (and the database) holds."""
        self.current = self.build(clock or self.clock, options)
        return self.current

    async def row(self, day: date, installation_id: int) -> Row:
        assert self.current is not None
        return await self.read(self.current, day, installation_id)


def _memory() -> Harness:
    async def read(ledger: UsageLedger, day: date, installation_id: int) -> Row:
        assert isinstance(ledger, InMemoryUsageLedger)
        return ledger.usage(day, installation_id)

    return Harness(
        clock=FakeClock(T0),
        build=lambda clock, options: InMemoryUsageLedger(clock, **options),
        read=read,
    )


@asynccontextmanager
async def _postgres(url: str) -> AsyncIterator[Harness]:
    engine = make_engine(url)

    async def read(ledger: UsageLedger, day: date, installation_id: int) -> Row:
        assert isinstance(ledger, PostgresUsageLedger)
        return await ledger.usage(day, installation_id)

    try:
        yield Harness(
            clock=FakeClock(T0),
            build=lambda clock, options: PostgresUsageLedger(engine, clock, **options),
            read=read,
        )
    finally:
        await engine.dispose()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    if request.param == "memory":
        yield _memory()
        return
    url: str = request.getfixturevalue("pg_clean")
    async with _postgres(url) as postgres:
        yield postgres


async def test_a_reservation_holds_the_estimate_and_counts_a_call(harness: Harness) -> None:
    ledger = harness.ledger()

    reservation = await ledger.reserve(1, CENT)

    assert reservation is not None
    assert (reservation.installation_id, reservation.day) == (1, T0.date())
    assert reservation.est_cost_usd == CENT
    row = await harness.row(T0.date(), 1)
    assert (row.calls, row.reserved_usd, row.spent_usd) == (1, CENT, Decimal(0))


async def test_settling_swaps_the_estimate_for_the_real_cost(harness: Harness) -> None:
    ledger = harness.ledger()
    reservation = await ledger.reserve(1, CENT)
    assert reservation is not None

    await ledger.settle(reservation, Decimal("0.004"))

    row = await harness.row(T0.date(), 1)
    assert (row.calls, row.reserved_usd, row.spent_usd) == (1, Decimal(0), Decimal("0.004"))


async def test_an_installation_has_a_daily_call_cap(harness: Harness) -> None:
    ledger = harness.ledger(calls_per_installation=2)

    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is None  # the third call of the day
    assert await ledger.reserve(2, CENT) is not None  # another installation is not affected
    assert (await harness.row(T0.date(), 1)).calls == 2  # a refusal is not a call


async def test_the_global_budget_counts_held_and_spent_money(harness: Harness) -> None:
    ledger = harness.ledger(daily_budget_usd=Decimal("0.05"))
    first = await ledger.reserve(1, Decimal("0.03"))
    assert first is not None

    assert await ledger.reserve(2, Decimal("0.03")) is None  # 0.03 held + 0.03 > 0.05
    await ledger.settle(first, Decimal("0.01"))  # the real cost was lower: money is free again
    assert await ledger.reserve(2, Decimal("0.03")) is not None
    assert await ledger.reserve(3, Decimal("0.02")) is None  # 0.01 + 0.03 held + 0.02 > 0.05


async def test_the_budget_is_the_sum_of_the_day_across_installations(harness: Harness) -> None:
    ledger = harness.ledger(daily_budget_usd=Decimal("0.05"))

    assert await ledger.reserve(1, Decimal("0.02")) is not None
    assert await ledger.reserve(2, Decimal("0.02")) is not None
    assert await ledger.reserve(3, Decimal("0.02")) is None  # 0.04 + 0.02 > 0.05
    assert await ledger.reserve(3, Decimal("0.01")) is not None  # exactly the budget fits
    assert (await harness.row(T0.date(), 3)).calls == 1  # the refused one did not count


async def test_a_budget_of_zero_refuses_everything_even_a_free_call(harness: Harness) -> None:
    ledger = harness.ledger(daily_budget_usd=Decimal(0))

    assert await ledger.reserve(1, Decimal(0)) is None  # LLM_DAILY_BUDGET_USD=0 means no LLM
    assert (await harness.row(T0.date(), 1)).calls == 0


async def test_a_cap_of_zero_calls_refuses_everything(harness: Harness) -> None:
    assert await harness.ledger(calls_per_installation=0).reserve(1, Decimal(0)) is None
    assert (await harness.row(T0.date(), 1)).calls == 0


async def test_free_calls_are_only_limited_by_the_call_cap(harness: Harness) -> None:
    ledger = harness.ledger(calls_per_installation=3)

    reservations = [await ledger.reserve(1, Decimal(0)) for _ in range(4)]

    assert [r is not None for r in reservations] == [True, True, True, False]


async def test_a_new_utc_day_starts_with_empty_limits(harness: Harness) -> None:
    ledger = harness.ledger(calls_per_installation=1, daily_budget_usd=CENT)
    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is None

    harness.clock.advance(timedelta(days=1))

    assert await ledger.reserve(1, CENT) is not None


async def test_the_day_is_the_utc_day(harness: Harness) -> None:
    just_before_midnight = datetime(2026, 11, 12, 23, 59, tzinfo=UTC)
    ledger = harness.ledger(clock=FakeClock(just_before_midnight), calls_per_installation=1)

    reservation = await ledger.reserve(1, CENT)

    assert reservation is not None
    assert reservation.day == just_before_midnight.date()


async def test_settling_after_midnight_updates_the_day_of_the_reservation(
    harness: Harness,
) -> None:
    clock = FakeClock(datetime(2026, 11, 12, 23, 59, tzinfo=UTC))
    ledger = harness.ledger(clock=clock)
    reservation = await ledger.reserve(1, CENT)
    assert reservation is not None

    clock.advance(timedelta(minutes=5))
    await ledger.settle(reservation, Decimal("0.004"))

    assert (await harness.row(reservation.day, 1)).spent_usd == Decimal("0.004")
    assert (await harness.row(reservation.day + timedelta(days=1), 1)).spent_usd == Decimal(0)


async def test_settling_a_reservation_that_was_never_made_is_an_error(harness: Harness) -> None:
    ledger = harness.ledger()

    with pytest.raises(KeyError):
        await ledger.settle(Reservation(installation_id=9, day=T0.date(), est_cost_usd=CENT), CENT)
