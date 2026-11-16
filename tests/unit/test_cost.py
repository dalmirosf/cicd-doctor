"""LLM cost accounting (FR-13, D-16)."""

from decimal import Decimal

from cicd_doctor.core.diagnosis.cost import MAX_CALLS_PER_JOB, PROMPT_OVERHEAD_TOKENS, CostModel

PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))


def test_a_call_costs_its_tokens_at_the_per_million_prices() -> None:
    assert PRICES.cost(1_000_000, 0) == Decimal("0.400000")
    assert PRICES.cost(0, 1_000_000) == Decimal("1.600000")
    assert PRICES.cost(5000, 400) == Decimal("0.002640")


def test_the_cost_is_rounded_up_to_six_decimals() -> None:
    assert PRICES.cost(1, 0) == Decimal("0.000001")  # 0.0000004 is held as a full millionth
    assert PRICES.cost(0, 0) == Decimal("0.000000")


def test_without_prices_everything_is_free() -> None:
    free = CostModel()

    assert free.cost(10_000, 800) == Decimal(0)
    assert free.reservation(6000, 800) == Decimal(0)


def test_a_reservation_covers_the_call_and_its_repair() -> None:
    reserved = PRICES.reservation(excerpt_tokens=6000, max_output_tokens=800)

    one_call = PRICES.cost(6000 + PROMPT_OVERHEAD_TOKENS, 800)
    assert reserved == MAX_CALLS_PER_JOB * one_call
    assert reserved > PRICES.cost(6000 + 300, 600) * 2  # above what a typical call costs
