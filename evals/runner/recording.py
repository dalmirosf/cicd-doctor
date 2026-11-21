"""What an eval run records of the model, and what stops it from spending too much (D-100, D-108).

Evals only: nothing here is in ``core/``. ``RecordingLLM`` keeps, per case, the answers that came
back (raw JSON, real latency, tokens, model) and how many calls the case needed. ``SpendGuard``
and ``GuardedLLM`` stop a live run at ``--max-cost-usd`` or ``--max-calls``: the answers already
written stay in ``evals/answers/``.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from cicd_doctor.core.ports import LLMClient


class BudgetExceeded(Exception):  # noqa: N818 - a stop signal, not an error of the model
    """A live run reached a cap. Not an ``LLMError``: it must not become a fallback."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class CaseRecording:
    """The calls one case made. ``accepted`` is the last answer: a repair call comes after."""

    calls: int = 0
    failed: int = 0
    results: list[LLMResult] = field(default_factory=list)

    @property
    def accepted(self) -> LLMResult | None:
        return self.results[-1] if self.results else None


class RecordingLLM:
    """Implements ``LLMClient``. Call ``begin(case_id)`` before each case."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self._current = ""
        self.cases: dict[str, CaseRecording] = {}

    def begin(self, case_id: str) -> None:
        self._current = case_id
        self.cases[case_id] = CaseRecording()

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        recording = self.cases.setdefault(self._current, CaseRecording())
        recording.calls += 1
        try:
            result = await self._inner.diagnose(request)
        except LLMError:
            recording.failed += 1
            raise
        recording.results.append(result)
        return result


class SpendGuard:
    """Shared by every live client of a run: counts calls and cost, refuses past a cap."""

    def __init__(self, *, max_cost_usd: Decimal, max_calls: int) -> None:
        if max_cost_usd <= 0 or max_calls <= 0:
            raise ValueError("the caps must be positive")
        self.max_cost_usd = max_cost_usd
        self.max_calls = max_calls
        self.calls = 0
        self.cost_usd = Decimal(0)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"reached --max-calls {self.max_calls}")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"reached --max-cost-usd {self.max_cost_usd}")

    def add(self, cost: Decimal) -> None:
        self.calls += 1
        self.cost_usd += cost


class GuardedLLM:
    """Wraps the live client (inside the cache, so a cache hit costs nothing and counts nothing)."""

    def __init__(self, inner: LLMClient, guard: SpendGuard, cost: CostModel) -> None:
        self._inner = inner
        self._guard = guard
        self._cost = cost

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        self._guard.check()
        try:
            result = await self._inner.diagnose(request)
        except LLMError:
            # The provider may have answered and been billed (a refusal, an incomplete or invalid
            # response). An LLMError carries no usage, so the cost is unknown: the call is counted
            # (--max-calls stays an upper bound) and the cost is not (D-115).
            self._guard.add(Decimal(0))
            raise
        self._guard.add(self._cost.cost(result.input_tokens, result.output_tokens))
        return result
