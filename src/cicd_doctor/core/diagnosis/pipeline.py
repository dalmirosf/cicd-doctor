"""One failed job's diagnosis: policy, budget, LLM call, validation, fallbacks (SPEC §7.1, D-66).

``DiagnoseJob.run`` takes the offline analysis of a job (excerpt, rules, signature) and decides
what to publish. The rules diagnosis always exists. The LLM is used only when the policy allows
it and the budget can be reserved; whatever goes wrong, the answer is the rules diagnosis with a
``fallback_reason``, never an exception: an LLM outage never blocks a diagnosis (NFR-12).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from cicd_doctor.core.analysis import JobAnalysis
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.diagnosis.validate import InvalidOutputError, finalize, parse_output
from cicd_doctor.core.errors import LLMError, LLMTransientError
from cicd_doctor.core.models import (
    Diagnosis,
    DiagnosisRequest,
    FallbackReason,
    LLMResult,
)
from cicd_doctor.core.policy import PolicyInput, decide
from cicd_doctor.core.ports import LLMClient, UsageLedger

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
DEFAULT_BACKOFFS = (1.0, 4.0)  # seconds before the 2nd and the 3rd try of a call (SPEC §7.1, §8)


@dataclass(frozen=True)
class DiagnosisContext:
    """What the pipeline needs to know about the run, besides the job itself."""

    installation_id: int
    is_fork: bool
    llm_enabled: bool = True  # LLM_ENABLED
    repo_llm: Literal["auto", "on_demand", "off"] = "auto"  # RepoConfig.llm
    explain: bool = False  # triggered by `/cicd-doctor explain` (P3)
    cached: Diagnosis | None = None  # the cached LLM diagnosis of the same signature (P2)


@dataclass
class LLMUsage:
    """What the calls of one job used, for the ``diagnoses`` row and the ledger."""

    model: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal(0)
    failed_calls: int = 0  # attempts that raised (retried or not): they cost nothing

    def add(self, result: LLMResult, cost: Decimal) -> None:
        self.model = result.model
        self.calls += 1
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.latency_ms += result.latency_ms
        self.cost_usd += cost


@dataclass(frozen=True)
class JobDiagnosis:
    published: Diagnosis  # what the check run and the comment show
    rules: Diagnosis  # always computed (D-35); equal to ``published`` minus the reason on fallback
    usage: LLMUsage | None = None  # None when no call was made
    notes: list[str] = field(default_factory=list)  # why a fallback happened, for logs and tests


class DiagnoseJob:
    def __init__(
        self,
        *,
        llm: LLMClient | None,
        ledger: UsageLedger,
        cost: CostModel,
        max_output_tokens: int,
        sleep: Sleep = asyncio.sleep,
        backoffs: Sequence[float] = DEFAULT_BACKOFFS,
    ) -> None:
        self._llm = llm
        self._ledger = ledger
        self._cost = cost
        self._max_output_tokens = max_output_tokens
        self._sleep = sleep
        self._backoffs = tuple(backoffs)

    async def run(
        self, analysis: JobAnalysis, context: DiagnosisContext, *, use_hints: bool = True
    ) -> JobDiagnosis:
        """Publish the LLM diagnosis when allowed and valid, else the rules one with a reason.

        ``use_hints=False`` withholds the rule matches from the prompt (the ``llm`` eval variant,
        D-14).
        """
        rules = analysis.rules
        decision = decide(
            PolicyInput(
                llm_enabled=context.llm_enabled,
                repo_llm=context.repo_llm,
                is_fork=context.is_fork,
                explain=context.explain,
                cache_hit=context.cached is not None,
            )
        )
        if decision.action == "cache":  # the policy only says "cache" when one is available
            assert context.cached is not None
            return JobDiagnosis(context.cached.model_copy(update={"source": "cache"}), rules)
        if decision.action == "rules":
            assert decision.fallback_reason is not None
            return self._fallback(rules, decision.fallback_reason)
        if self._llm is None:  # no key configured: the same as the kill switch
            return self._fallback(rules, "disabled")

        estimate = self._cost.reservation(analysis.excerpt.token_count, self._max_output_tokens)
        reservation = await self._ledger.reserve(context.installation_id, estimate)
        if reservation is None:
            return self._fallback(rules, "budget")

        usage = LLMUsage()
        try:
            return await self._diagnose(analysis, use_hints, usage)
        finally:
            # Settle with what was really spent, also when the call is cancelled or a bug raises.
            await self._ledger.settle(reservation, usage.cost_usd)

    async def _diagnose(
        self, analysis: JobAnalysis, use_hints: bool, usage: LLMUsage
    ) -> JobDiagnosis:
        rules = analysis.rules
        request = DiagnosisRequest(
            job=analysis.job,
            excerpt=analysis.excerpt,
            rule_hints=list(analysis.matches) if use_hints else [],
        )
        error: str | None = None
        for attempt in (1, 2):  # the call and, at most, one repair of an invalid answer
            if attempt == 2:
                request = request.model_copy(update={"repair_error": error})
            try:
                result = await self._call(request, usage)
            except LLMError as exc:
                logger.warning("LLM call failed (%s), publishing the rules diagnosis", type(exc))
                return self._fallback(rules, "llm_error", usage, f"{type(exc).__name__}")
            try:
                output = parse_output(result.raw_json)
                diagnosis = finalize(
                    output, analysis.excerpt, model=result.model, prompt_version=PROMPT_VERSION
                )
            except InvalidOutputError as exc:
                error = str(exc)
                logger.info("LLM answer %d is invalid: %s", attempt, error)
                continue
            return JobDiagnosis(diagnosis, rules, usage)
        return self._fallback(rules, "invalid_output", usage, error or "")

    async def _call(self, request: DiagnosisRequest, usage: LLMUsage) -> LLMResult:
        """One call, retried on transient errors after each backoff delay."""
        assert self._llm is not None
        for delay in (*self._backoffs, None):
            try:
                result = await self._llm.diagnose(request)
            except LLMTransientError:
                usage.failed_calls += 1
                if delay is None:
                    raise
                await self._sleep(delay)
                continue
            except LLMError:
                usage.failed_calls += 1
                raise
            usage.add(result, self._cost.cost(result.input_tokens, result.output_tokens))
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _fallback(
        rules: Diagnosis,
        reason: FallbackReason,
        usage: LLMUsage | None = None,
        note: str = "",
    ) -> JobDiagnosis:
        published = rules.model_copy(update={"fallback_reason": reason})
        return JobDiagnosis(published, rules, usage, [note] if note else [])
