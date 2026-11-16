"""LLM cost accounting (SPEC FR-13, D-16): what a call costs and how much to reserve for it."""

from decimal import Decimal

from cicd_doctor.core.models import DomainModel

MILLION = Decimal(1_000_000)
# System prompt, trusted metadata and rule hints around the excerpt, in tokens. A generous
# constant: the reservation only has to be an upper bound of what the call will cost.
PROMPT_OVERHEAD_TOKENS = 1500
MAX_CALLS_PER_JOB = 2  # the diagnosis call and, at most, one schema-repair call (D-16)


class CostModel(DomainModel):
    """USD per million tokens, from ``LLM_PRICE_INPUT_PER_MTOK`` and ``LLM_PRICE_OUTPUT_PER_MTOK``.

    Without prices (the settings leave them optional) every call costs 0, so the budget cannot
    stop anything; the pipeline logs that and the daily call limit still applies.
    """

    input_per_mtok: Decimal = Decimal(0)
    output_per_mtok: Decimal = Decimal(0)

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """The cost of one call, rounded up to 6 decimals like ``llm_usage`` (SPEC §6)."""
        raw = (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / MILLION
        return raw.quantize(Decimal("0.000001"), rounding="ROUND_UP")

    def reservation(self, excerpt_tokens: int, max_output_tokens: int) -> Decimal:
        """An upper bound of the cost of a job: the call and its possible repair (D-16)."""
        return MAX_CALLS_PER_JOB * self.cost(
            excerpt_tokens + PROMPT_OVERHEAD_TOKENS, max_output_tokens
        )
