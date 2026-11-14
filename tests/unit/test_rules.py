"""Rule-based diagnoser (SPEC §10, D-60): every rule against its positive and negative fixtures,
plus the engine's ranking, templates and the unknown result."""

import re
from collections import Counter

import pytest

from cicd_doctor.core.models import Category
from cicd_doctor.core.rules import RULE_PATTERNS, RULES, Rule, match_rule, match_rules
from cicd_doctor.core.rules.engine import (
    MAX_EVIDENCE,
    MAX_GROUP_CHARS,
    UNKNOWN_CAUSE,
    RuleContext,
    compile_all,
    rules_diagnosis,
)
from tests.rulecases import RULE_CASES_DIR, RuleCase, load_rule_cases

CASES = load_rule_cases()
RULES_BY_ID = {rule.id: rule for rule in RULES}


def test_rule_ids_are_unique_and_match_the_spec_table() -> None:
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert len(ids) == 29  # SPEC §10


def test_every_rule_has_a_positive_and_a_negative_fixture() -> None:
    kinds = Counter((case.rule_id, case.kind) for case in CASES)

    missing = [
        f"{rule.id}:{kind}"
        for rule in RULES
        for kind in ("positive", "negative")
        if kinds[(rule.id, kind)] < 1
    ]

    assert missing == []
    assert {path.stem for path in RULE_CASES_DIR.glob("*.txt")} == set(RULES_BY_ID)


def test_rules_never_output_flaky() -> None:
    assert all(rule.category is not Category.FLAKY for rule in RULES)


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.kind == "positive"], ids=lambda c: c.case_id
)
def test_positive_fixture(case: RuleCase) -> None:
    rule = RULES_BY_ID[case.rule_id]
    context = RuleContext(job=case.job(), excerpt=case.excerpt())

    match = match_rule(rule, context)
    ranked = match_rules(case.job(), case.excerpt(), RULES)

    assert match is not None
    assert ranked[0].rule_id == case.metadata.get("top", case.rule_id)
    if "category" in case.metadata:
        assert match.category.value == case.metadata["category"]
    if "expect-cause" in case.metadata:
        assert case.metadata["expect-cause"] in match.cause
    assert "{" not in match.cause
    assert "{" not in match.fix
    for number in match.evidence_lines:
        assert any(p.search(case.lines[number - 1]) for p in rule.all_patterns())


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.kind == "negative"], ids=lambda c: c.case_id
)
def test_negative_fixture(case: RuleCase) -> None:
    rule = RULES_BY_ID[case.rule_id]

    assert match_rule(rule, RuleContext(job=case.job(), excerpt=case.excerpt())) is None


def _case(rule_id: str, kind: str = "positive", index: int = 0) -> RuleCase:
    return [c for c in CASES if c.rule_id == rule_id and c.kind == kind][index]


def test_a_more_specific_rule_outranks_a_symptom() -> None:
    lines = [
        "FAILED tests/test_db.py::test_query - ConnectionRefusedError: Connection refused",
        "E   ModuleNotFoundError: No module named 'psycopg'",
    ]
    case = RuleCase("PY-MODNF", "positive", "mix", lines)

    ranked = [m.rule_id for m in match_rules(case.job(), case.excerpt(), RULES)]

    assert ranked[0] == "PY-MODNF"  # priority 2 beats the priority-1 symptoms
    assert set(ranked) == {"PY-MODNF", "PY-PYTEST-FAIL", "GHA-CONN"}


def test_ties_go_to_the_higher_confidence_then_the_earliest_line() -> None:
    a = Rule("A", "x", compile_all("alpha"), Category.NETWORK, 0.5, 1, "a", "a")
    b = Rule("B", "x", compile_all("beta"), Category.NETWORK, 0.5, 1, "b", "b")
    c = Rule("C", "x", compile_all("gamma"), Category.NETWORK, 0.9, 1, "c", "c")
    case = RuleCase("A", "positive", "tie", ["beta", "alpha", "gamma"])

    ranked = match_rules(case.job(), case.excerpt(), [a, b, c])

    assert [m.rule_id for m in ranked] == ["C", "B", "A"]


def test_evidence_is_capped_and_captured_values_are_trimmed() -> None:
    module = "m" * (MAX_GROUP_CHARS + 50)
    lines = [f"ModuleNotFoundError: No module named '{module}'"] * (MAX_EVIDENCE + 4)
    case = RuleCase("PY-MODNF", "positive", "many", lines)

    match = match_rule(RULES_BY_ID["PY-MODNF"], RuleContext(case.job(), case.excerpt()))

    assert match is not None
    assert len(match.evidence_lines) == MAX_EVIDENCE
    assert "m" * MAX_GROUP_CHARS in match.cause
    assert "m" * (MAX_GROUP_CHARS + 1) not in match.cause


def test_a_template_without_a_captured_value_gets_its_default() -> None:
    case = RuleCase("PY-PYTEST-FAIL", "positive", "summary", ["===== 2 failed in 1.0s ====="])

    match = match_rule(RULES_BY_ID["PY-PYTEST-FAIL"], RuleContext(case.job(), case.excerpt()))

    assert match is not None
    assert match.cause == "The test `suite` failed."


def test_a_placeholder_without_value_or_default_becomes_a_question_mark() -> None:
    rule = Rule("X", "x", compile_all("boom"), Category.UNKNOWN, 0.1, 1, "{nothing} failed", "-")
    case = RuleCase("X", "positive", "x", ["boom"])

    match = match_rule(rule, RuleContext(case.job(), case.excerpt()))

    assert match is not None
    assert match.cause == "? failed"


def test_log_text_in_a_captured_value_is_not_interpreted_as_a_template() -> None:
    case = RuleCase(
        "PY-MODNF", "positive", "braces", ["ModuleNotFoundError: No module named '{fix}'"]
    )

    match = match_rule(RULES_BY_ID["PY-MODNF"], RuleContext(case.job(), case.excerpt()))

    assert match is not None
    assert "`{fix}`" in match.cause


def test_rules_diagnosis_takes_the_best_match() -> None:
    case = _case("PY-MODNF")
    matches = match_rules(case.job(), case.excerpt(), RULES)

    diagnosis = rules_diagnosis(case.excerpt(), matches)

    assert diagnosis.source == "rules"
    assert diagnosis.category is Category.DEPENDENCY
    assert diagnosis.confidence == pytest.approx(0.90)
    assert diagnosis.rule_ids[0] == "PY-MODNF"
    assert [e.line for e in diagnosis.evidence] == matches[0].evidence_lines
    assert diagnosis.evidence[0].text == "ModuleNotFoundError: No module named 'requests'"
    assert diagnosis.evidence_verified is True


def test_rules_diagnosis_without_a_match_is_unknown_with_zero_confidence() -> None:
    case = RuleCase("X", "positive", "nothing", ["E9921: the frobnicator rejected the manifest"])

    diagnosis = rules_diagnosis(case.excerpt(), match_rules(case.job(), case.excerpt(), RULES))

    assert diagnosis.category is Category.UNKNOWN
    assert diagnosis.confidence == 0.0
    assert diagnosis.probable_cause == UNKNOWN_CAUSE
    assert diagnosis.evidence == []
    assert diagnosis.rule_ids == []


def test_long_evidence_lines_are_cut_to_the_evidence_limit() -> None:
    line = "ModuleNotFoundError: No module named 'x' " + "y" * 400
    case = RuleCase("PY-MODNF", "positive", "long", [line])

    diagnosis = rules_diagnosis(case.excerpt(), match_rules(case.job(), case.excerpt(), RULES))

    assert len(diagnosis.evidence[0].text) == 300


def test_rule_patterns_are_all_exported_for_error_line_detection() -> None:
    assert len(RULE_PATTERNS) == sum(len(rule.all_patterns()) for rule in RULES)
    assert all(isinstance(pattern, re.Pattern) for pattern in RULE_PATTERNS)


def test_rule_patterns_survive_pathological_input_quickly() -> None:
    import time

    line = "Error: " + "token " * 400 + "x" * 2000  # long, repetitive, no match at the end
    case = RuleCase("X", "positive", "slow", [line] * 50)

    started = time.perf_counter()
    match_rules(case.job(), case.excerpt(), RULES)

    assert time.perf_counter() - started < 2.0
