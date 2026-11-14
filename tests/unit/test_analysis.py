"""The offline analysis (excerpt + rules + signature) on the synthetic log fixtures (D-60)."""

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.analysis import analyze_jobs
from cicd_doctor.core.logs import build_excerpts
from cicd_doctor.core.models import Category
from cicd_doctor.core.rules import Rule
from cicd_doctor.core.rules.engine import compile_all
from cicd_doctor.core.signature import compute_signature
from tests.logcases import load_log_case, select_case_jobs

COUNTER = CharTokenCounter()

# The rule each synthetic fixture should trigger first, and the category it gives.
EXPECTED = {
    "pytest-assert": ("PY-PYTEST-FAIL", Category.BROKEN_TEST),
    "py-modnf": ("PY-MODNF", Category.DEPENDENCY),
    "job-timeout": ("GHA-TIMEOUT", Category.TIMEOUT),
    "docker-copy": ("DOCKER-COPY", Category.CONFIG_OR_SECRETS),
    "no-step-data": (None, Category.UNKNOWN),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_gets_the_expected_rules_diagnosis(name: str) -> None:
    case = load_log_case(name)
    jobs = select_case_jobs(case)

    [analysis] = analyze_jobs([(job, case.raw_log(job.job.job_id)) for job in jobs], COUNTER)

    rule_id, category = EXPECTED[name]
    assert analysis.rules.category is category
    assert analysis.rules.source == "rules"
    assert (analysis.matches[0].rule_id if analysis.matches else None) == rule_id
    if rule_id is not None:
        assert analysis.signature.text.split("\n")[3] == rule_id  # the signature's error type
    excerpt_lines = {line.number for line in analysis.excerpt.lines}
    assert {e.line for e in analysis.rules.evidence} <= excerpt_lines


def test_the_signature_uses_the_top_rule_id() -> None:
    case = load_log_case("py-modnf")
    [job] = select_case_jobs(case)

    [analysis] = analyze_jobs([(job, case.raw_log(job.job.job_id))], COUNTER)

    assert analysis.signature == compute_signature(job, analysis.excerpt, "PY-MODNF")


def test_rule_patterns_keep_rule_lines_in_the_excerpt() -> None:
    # "npm error 404 Not Found" is not a generic error line; with the rule patterns it would
    # only be kept by the tail window. A mypy line is kept because PY-MYPY's pattern flags it.
    case = load_log_case("pytest-assert")
    [job] = select_case_jobs(case)
    raw = case.raw_log(job.job.job_id)
    mypy_line = "src/app.py:4: error: Incompatible return value type  [return-value]"
    text = raw.text.replace("cachedir: .pytest_cache", mypy_line)
    raw = raw.model_copy(update={"text": text})

    plain = build_excerpts([(job, raw)], COUNTER)[0]
    [analysis] = analyze_jobs([(job, raw)], COUNTER)

    number = next(line.number for line in analysis.excerpt.lines if line.text == mypy_line)
    assert number in analysis.excerpt.error_lines
    assert number not in plain.error_lines


def test_jobs_share_one_budget() -> None:
    case = load_log_case("multi-job")
    jobs = select_case_jobs(case)

    analyses = analyze_jobs([(job, case.raw_log(job.job.job_id)) for job in jobs], COUNTER)

    assert [a.job.job.job_id for a in analyses] == case.selected_job_ids
    assert len({a.signature.hash for a in analyses}) == 3
    assert all(a.rules.category is Category.BROKEN_TEST for a in analyses)


def test_a_custom_rule_set_brings_its_own_patterns() -> None:
    case = load_log_case("no-step-data")
    [job] = select_case_jobs(case)
    npm_rule = Rule(
        id="NPM-404",
        ecosystem="node",
        patterns=compile_all(r"npm error 404 Not Found"),
        category=Category.DEPENDENCY,
        confidence=0.9,
        priority=2,
        cause="A package is missing from the npm registry.",
        fix="Check the package name.",
    )

    [analysis] = analyze_jobs([(job, case.raw_log(job.job.job_id))], COUNTER, rules=[npm_rule])

    assert analysis.rules.category is Category.DEPENDENCY
    assert analysis.rules.rule_ids == ["NPM-404"]
