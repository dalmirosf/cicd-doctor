"""P1b acceptance: every captured lab case fits in 6,000 real tokens (SPEC §13, D-57)."""

import pytest

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.analysis import analyze_jobs
from cicd_doctor.core.logs import ExcerptConfig
from evals.runner.cases import Case, load_cases
from evals.runner.variants import pick_job

LAB_CASES = load_cases(sources=["lab"])


@pytest.mark.parametrize("case", LAB_CASES, ids=lambda case: case.id)
def test_lab_case_excerpt_is_within_the_token_budget(case: Case) -> None:
    job = pick_job(case)
    assert job is not None

    [analysis] = analyze_jobs([(job, case.log)], TiktokenCounter())

    assert analysis.excerpt.token_count <= ExcerptConfig().max_tokens
