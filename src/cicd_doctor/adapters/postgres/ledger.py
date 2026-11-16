"""Postgres ``UsageLedger`` (FR-13, D-16, SPEC §6 *Budget reservation*, D-69).

``reserve`` runs in one transaction that holds ``pg_advisory_xact_lock(hashtext('llm_budget'))``,
so concurrent workers (and processes) are serialised: they check the installation's call cap and
the global budget of the UTC day against rows that already include every earlier reservation,
and only then count the call and hold the estimate. ``settle`` is one atomic ``UPDATE`` that moves
the hold to ``spent_usd``. The UTC day comes from the injected ``Clock`` (D-48).

A reservation of a worker that dies before ``settle`` stays held until the day ends: conservative.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date
from decimal import Decimal
from typing import NamedTuple, Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cicd_doctor.adapters.postgres.engine import make_engine, transient_db_errors
from cicd_doctor.core.models import Reservation
from cicd_doctor.core.ports import Clock

_LOCK_BUDGET = text("SELECT pg_advisory_xact_lock(hashtext('llm_budget'))")
_INSTALLATION_CALLS = text(
    "SELECT calls FROM llm_usage WHERE day = :day AND installation_id = :installation_id"
)
_COMMITTED_TODAY = text(
    "SELECT COALESCE(SUM(reserved_usd + spent_usd), 0) FROM llm_usage WHERE day = :day"
)
_HOLD = text(
    """
    INSERT INTO llm_usage (day, installation_id, calls, reserved_usd)
    VALUES (:day, :installation_id, 1, :est_cost_usd)
    ON CONFLICT (day, installation_id) DO UPDATE
    SET calls = llm_usage.calls + 1, reserved_usd = llm_usage.reserved_usd + EXCLUDED.reserved_usd
    """
)
_SETTLE = text(
    """
    UPDATE llm_usage
    SET reserved_usd = reserved_usd - :est_cost_usd, spent_usd = spent_usd + :actual_cost_usd
    WHERE day = :day AND installation_id = :installation_id
    """
)
_USAGE = text(
    """
    SELECT calls, reserved_usd, spent_usd FROM llm_usage
    WHERE day = :day AND installation_id = :installation_id
    """
)


class UsageRow(NamedTuple):
    """One ``llm_usage`` row, for tests and operations (the in-memory ledger's has the same)."""

    calls: int = 0
    reserved_usd: Decimal = Decimal(0)
    spent_usd: Decimal = Decimal(0)


class PostgresUsageLedger:
    """Implements ``UsageLedger``: a daily call cap per installation and a global daily budget.

    A budget of 0, or a cap of 0 calls, refuses everything without touching the database (a free
    call is refused too: ``LLM_DAILY_BUDGET_USD=0`` means no LLM). Create it with
    ``PostgresUsageLedger.connect(...)``.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        clock: Clock,
        *,
        calls_per_installation: int = 30,  # LLM_DAILY_CALLS_PER_INSTALLATION
        daily_budget_usd: Decimal = Decimal("2.00"),  # LLM_DAILY_BUDGET_USD
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._calls_per_installation = calls_per_installation
        self._daily_budget_usd = daily_budget_usd

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        database_url: str,
        clock: Clock,
        *,
        calls_per_installation: int = 30,
        daily_budget_usd: Decimal = Decimal("2.00"),
        pool_size: int = 5,
    ) -> AsyncIterator[Self]:
        """``database_url`` is a SQLAlchemy URL (``postgresql+psycopg://...``)."""
        engine = make_engine(database_url, pool_size=pool_size)
        try:
            yield cls(
                engine,
                clock,
                calls_per_installation=calls_per_installation,
                daily_budget_usd=daily_budget_usd,
            )
        finally:
            await engine.dispose()

    async def reserve(self, installation_id: int, est_cost_usd: Decimal) -> Reservation | None:
        if self._daily_budget_usd <= 0 or self._calls_per_installation <= 0:
            return None
        day = self._clock.now().astimezone(UTC).date()
        key = {"day": day, "installation_id": installation_id}
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(_LOCK_BUDGET)  # released when the transaction ends
                calls = (await conn.execute(_INSTALLATION_CALLS, key)).scalar() or 0
                if calls >= self._calls_per_installation:
                    return None
                committed = (await conn.execute(_COMMITTED_TODAY, {"day": day})).scalar_one()
                if committed + est_cost_usd > self._daily_budget_usd:
                    return None
                await conn.execute(_HOLD, {**key, "est_cost_usd": est_cost_usd})
        return Reservation(installation_id=installation_id, day=day, est_cost_usd=est_cost_usd)

    async def settle(self, reservation: Reservation, actual_cost_usd: Decimal) -> None:
        params = {
            "day": reservation.day,
            "installation_id": reservation.installation_id,
            "est_cost_usd": reservation.est_cost_usd,
            "actual_cost_usd": actual_cost_usd,
        }
        with transient_db_errors():
            async with self._engine.begin() as conn:
                result = await conn.execute(_SETTLE, params)
        if result.rowcount != 1:
            raise KeyError(
                f"no usage row for installation {reservation.installation_id} on {reservation.day}"
            )

    async def usage(self, day: date, installation_id: int) -> UsageRow:
        """The row of a day and installation (an empty one when nothing was reserved)."""
        params = {"day": day, "installation_id": installation_id}
        with transient_db_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(_USAGE, params)).first()
        return UsageRow() if row is None else UsageRow(*row)
