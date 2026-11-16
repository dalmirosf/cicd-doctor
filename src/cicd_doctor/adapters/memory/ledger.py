"""In-memory ``UsageLedger``: reference behaviour for the contract tests (SPEC §6 ``llm_usage``).

One row per (UTC day, installation), like the table: ``calls``, ``reserved_usd`` and
``spent_usd``. ``PostgresUsageLedger`` (``adapters/postgres/ledger.py``) must behave the same: the
contract suite runs against both (D-69).
"""

from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal

from cicd_doctor.core.models import Reservation
from cicd_doctor.core.ports import Clock


@dataclass
class UsageRow:
    calls: int = 0
    reserved_usd: Decimal = Decimal(0)
    spent_usd: Decimal = Decimal(0)


class InMemoryUsageLedger:
    """Implements ``UsageLedger``: a daily call cap per installation and a global daily budget.

    ``reserve`` counts one call and holds the estimated cost; ``settle`` swaps the held cost for
    the real one. A budget of 0, or a cap of 0 calls, refuses everything.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        calls_per_installation: int = 30,  # LLM_DAILY_CALLS_PER_INSTALLATION
        daily_budget_usd: Decimal = Decimal("2.00"),  # LLM_DAILY_BUDGET_USD
    ) -> None:
        self._clock = clock
        self._calls_per_installation = calls_per_installation
        self._daily_budget_usd = daily_budget_usd
        self._rows: dict[tuple[date, int], UsageRow] = {}

    async def reserve(self, installation_id: int, est_cost_usd: Decimal) -> Reservation | None:
        day = self._clock.now().astimezone(UTC).date()
        existing = self._rows.get((day, installation_id), UsageRow())
        if existing.calls >= self._calls_per_installation or self._daily_budget_usd <= 0:
            return None
        committed = sum(
            (r.reserved_usd + r.spent_usd for (d, _), r in self._rows.items() if d == day),
            Decimal(0),
        )
        if committed + est_cost_usd > self._daily_budget_usd:
            return None
        row = self._rows.setdefault((day, installation_id), existing)  # a refusal leaves no row
        row.calls += 1
        row.reserved_usd += est_cost_usd
        return Reservation(installation_id=installation_id, day=day, est_cost_usd=est_cost_usd)

    async def settle(self, reservation: Reservation, actual_cost_usd: Decimal) -> None:
        row = self._rows[(reservation.day, reservation.installation_id)]  # KeyError: not reserved
        row.reserved_usd -= reservation.est_cost_usd
        row.spent_usd += actual_cost_usd

    def usage(self, day: date, installation_id: int) -> UsageRow:
        """The row of a day and installation (an empty one when nothing was reserved)."""
        return self._rows.get((day, installation_id), UsageRow())
