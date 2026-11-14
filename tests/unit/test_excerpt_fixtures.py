"""Acceptance on the log fixtures (SPEC §13 P1b): selection, evidence lines, golden excerpts.

The token budget with real tiktoken counts is checked in ``tests/adapters/test_tiktoken.py``.
"""

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from tests.logcases import (
    case_excerpts,
    check_golden,
    load_log_case,
    log_case_names,
    render_excerpts,
    select_case_jobs,
)

CASES = log_case_names()
COUNTER = CharTokenCounter()


def test_there_are_log_cases() -> None:
    assert len(CASES) >= 7


@pytest.mark.parametrize("name", CASES)
def test_expected_jobs_are_selected(name: str) -> None:
    case = load_log_case(name)

    assert [job.job.job_id for job in select_case_jobs(case)] == case.selected_job_ids


@pytest.mark.parametrize("name", CASES)
def test_excerpt_contains_every_evidence_line(name: str) -> None:
    case = load_log_case(name)

    for excerpt in case_excerpts(case, COUNTER):
        kept = {line.number for line in excerpt.lines}
        expected = case.evidence_lines(excerpt.job.job_id)
        assert expected, "every selected job of a case lists its evidence lines"
        assert set(expected) <= kept, f"job {excerpt.job.job_id}: {sorted(set(expected) - kept)}"


@pytest.mark.parametrize("name", CASES)
def test_excerpt_is_consistent(name: str) -> None:
    for excerpt in case_excerpts(load_log_case(name), COUNTER):
        numbers = [line.number for line in excerpt.lines]
        assert numbers == sorted(set(numbers))
        assert set(excerpt.error_lines) <= set(numbers)
        assert excerpt.token_count <= 6000
        for start, end in excerpt.omitted:
            assert start <= end
            assert not any(start <= n <= end for n in numbers)


@pytest.mark.parametrize("name", CASES)
def test_excerpt_matches_its_golden_file(name: str) -> None:
    case = load_log_case(name)

    check_golden(case.root / "excerpt.golden.txt", render_excerpts(case_excerpts(case, COUNTER)))
