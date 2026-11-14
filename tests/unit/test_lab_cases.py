"""P1b acceptance on the captured lab cases (``evals/cases/lab``, SPEC §13).

Until the lab repository runs and its logs are captured there are no cases, and these tests are
skipped with "got empty parameter set". The token limit with real counts is in
``tests/adapters/test_lab_cases_tokens.py``.
"""

from collections import defaultdict

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.analysis import JobAnalysis, analyze_jobs
from evals.runner.cases import Case, load_cases
from evals.runner.variants import pick_job

LAB_CASES = load_cases(sources=["lab"])
COUNTER = CharTokenCounter()


def _analysis(case: Case) -> JobAnalysis:
    job = pick_job(case)
    assert job is not None, f"{case.id}: no failed job"
    [analysis] = analyze_jobs([(job, case.log)], COUNTER)
    return analysis


@pytest.mark.parametrize("case", LAB_CASES, ids=lambda case: case.id)
def test_excerpt_contains_every_expected_evidence_line(case: Case) -> None:
    analysis = _analysis(case)

    kept = {line.number for line in analysis.excerpt.lines}
    assert case.meta.expected.evidence_lines, f"{case.id}: no evidence lines were captured"
    assert set(case.meta.expected.evidence_lines) <= kept


def _signatures() -> dict[str, list[tuple[str, str]]]:
    by_lab_case: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for case in LAB_CASES:
        if case.meta.lab_case:
            by_lab_case[case.meta.lab_case].append((case.id, _analysis(case).signature.hash))
    return by_lab_case


REPEATED = (
    sorted(name for name, runs in _signatures().items() if len(runs) >= 2) if LAB_CASES else []
)


@pytest.mark.parametrize("lab_case", REPEATED)
def test_two_runs_of_one_failure_have_the_same_signature(lab_case: str) -> None:
    hashes = {signature for _, signature in _signatures()[lab_case]}

    assert len(hashes) == 1


@pytest.mark.skipif(len({c.meta.lab_case for c in LAB_CASES}) < 2, reason="needs 2+ lab cases")
def test_different_lab_failures_have_different_signatures() -> None:
    first_per_case = {name: runs[0][1] for name, runs in _signatures().items()}

    assert len(set(first_per_case.values())) == len(first_per_case)
