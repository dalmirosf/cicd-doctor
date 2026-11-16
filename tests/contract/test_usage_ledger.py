"""Contract suite for the ``UsageLedger`` port (FR-13, D-16, SPEC §6 ``llm_usage``).

Only the in-memory adapter exists; the Postgres one arrives with the diagnose use case (P1c) and
joins the fixture as an ``integration`` parameter (D-51).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory.ledger import InMemoryUsageLedger
from tests.factories import T0

CENT = Decimal("0.01")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


def make_ledger(clock: FakeClock, **options: object) -> InMemoryUsageLedger:
    return InMemoryUsageLedger(clock, **options)  # type: ignore[arg-type]


async def test_a_reservation_holds_the_estimate_and_counts_a_call(clock: FakeClock) -> None:
    ledger = make_ledger(clock)

    reservation = await ledger.reserve(1, CENT)

    assert reservation is not None
    assert (reservation.installation_id, reservation.day) == (1, T0.date())
    assert reservation.est_cost_usd == CENT
    row = ledger.usage(T0.date(), 1)
    assert (row.calls, row.reserved_usd, row.spent_usd) == (1, CENT, Decimal(0))


async def test_settling_swaps_the_estimate_for_the_real_cost(clock: FakeClock) -> None:
    ledger = make_ledger(clock)
    reservation = await ledger.reserve(1, CENT)
    assert reservation is not None

    await ledger.settle(reservation, Decimal("0.004"))

    row = ledger.usage(T0.date(), 1)
    assert (row.calls, row.reserved_usd, row.spent_usd) == (1, Decimal(0), Decimal("0.004"))


async def test_an_installation_has_a_daily_call_cap(clock: FakeClock) -> None:
    ledger = make_ledger(clock, calls_per_installation=2)

    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is None  # the third call of the day
    assert await ledger.reserve(2, CENT) is not None  # another installation is not affected


async def test_the_global_budget_counts_held_and_spent_money(clock: FakeClock) -> None:
    ledger = make_ledger(clock, daily_budget_usd=Decimal("0.05"))
    first = await ledger.reserve(1, Decimal("0.03"))
    assert first is not None

    assert await ledger.reserve(2, Decimal("0.03")) is None  # 0.03 held + 0.03 > 0.05
    await ledger.settle(first, Decimal("0.01"))  # the real cost was lower: money is free again
    assert await ledger.reserve(2, Decimal("0.03")) is not None
    assert await ledger.reserve(3, Decimal("0.02")) is None  # 0.01 + 0.03 held + 0.02 > 0.05


async def test_a_budget_of_zero_refuses_everything_even_a_free_call(clock: FakeClock) -> None:
    ledger = make_ledger(clock, daily_budget_usd=Decimal(0))

    assert await ledger.reserve(1, Decimal(0)) is None  # LLM_DAILY_BUDGET_USD=0 means no LLM


async def test_a_cap_of_zero_calls_refuses_everything(clock: FakeClock) -> None:
    assert await make_ledger(clock, calls_per_installation=0).reserve(1, Decimal(0)) is None


async def test_free_calls_are_only_limited_by_the_call_cap(clock: FakeClock) -> None:
    ledger = make_ledger(clock, calls_per_installation=3)

    reservations = [await ledger.reserve(1, Decimal(0)) for _ in range(4)]

    assert [r is not None for r in reservations] == [True, True, True, False]


async def test_a_new_utc_day_starts_with_empty_limits(clock: FakeClock) -> None:
    ledger = make_ledger(clock, calls_per_installation=1, daily_budget_usd=CENT)
    assert await ledger.reserve(1, CENT) is not None
    assert await ledger.reserve(1, CENT) is None

    clock.advance(timedelta(days=1))

    assert await ledger.reserve(1, CENT) is not None


async def test_the_day_is_the_utc_day(clock: FakeClock) -> None:
    just_before_midnight = datetime(2026, 11, 12, 23, 59, tzinfo=UTC)
    ledger = make_ledger(FakeClock(just_before_midnight), calls_per_installation=1)

    reservation = await ledger.reserve(1, CENT)

    assert reservation is not None
    assert reservation.day == just_before_midnight.date()


async def test_settling_after_midnight_updates_the_day_of_the_reservation() -> None:
    clock = FakeClock(datetime(2026, 11, 12, 23, 59, tzinfo=UTC))
    ledger = make_ledger(clock)
    reservation = await ledger.reserve(1, CENT)
    assert reservation is not None

    clock.advance(timedelta(minutes=5))
    await ledger.settle(reservation, Decimal("0.004"))

    assert ledger.usage(reservation.day, 1).spent_usd == Decimal("0.004")
    assert ledger.usage(reservation.day + timedelta(days=1), 1).spent_usd == Decimal(0)
