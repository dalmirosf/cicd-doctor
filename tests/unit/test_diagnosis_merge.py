"""Rules baseline versus the LLM diagnosis (SPEC §9.3 point 4)."""

import pytest

from cicd_doctor.core.diagnosis.merge import CONFIDENT_RULE, baseline_to_show, disagrees
from cicd_doctor.core.models import Category, Diagnosis


def _diagnosis(category: Category, confidence: float, source: str = "rules") -> Diagnosis:
    return Diagnosis(
        category=category,
        probable_cause="c",
        evidence=[],
        suggested_fix="f",
        confidence=confidence,
        source=source,  # type: ignore[arg-type]
    )


def test_a_confident_rule_that_disagrees_is_shown() -> None:
    rules = _diagnosis(Category.DEPENDENCY, 0.9)
    llm = _diagnosis(Category.UNKNOWN, 0.9, "llm")

    assert disagrees(rules, llm)
    assert baseline_to_show(rules, llm) is rules


@pytest.mark.parametrize(
    ("rules", "llm"),
    [
        (
            _diagnosis(Category.DEPENDENCY, 0.9),
            _diagnosis(Category.DEPENDENCY, 0.5, "llm"),
        ),  # agree
        (_diagnosis(Category.DEPENDENCY, 0.84), _diagnosis(Category.UNKNOWN, 0.9, "llm")),  # unsure
        (
            _diagnosis(Category.UNKNOWN, 0.0),
            _diagnosis(Category.DEPENDENCY, 0.9, "llm"),
        ),  # no match
        (_diagnosis(Category.DEPENDENCY, 0.9), _diagnosis(Category.UNKNOWN, 0.9)),  # LLM not used
        (_diagnosis(Category.DEPENDENCY, 0.9), _diagnosis(Category.UNKNOWN, 0.9, "cache")),
    ],
)
def test_otherwise_the_baseline_adds_nothing(rules: Diagnosis, llm: Diagnosis) -> None:
    assert not disagrees(rules, llm)
    assert baseline_to_show(rules, llm) is None


def test_the_threshold_is_inclusive() -> None:
    llm = _diagnosis(Category.UNKNOWN, 0.9, "llm")

    assert disagrees(_diagnosis(Category.DEPENDENCY, CONFIDENT_RULE), llm)
    assert CONFIDENT_RULE == 0.85
