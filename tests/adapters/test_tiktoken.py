"""Token counts with the real tokenizer (D-57). tiktoken downloads ``o200k_base`` on first use,
so these tests are not in the offline fast suite; CI runs them."""

from datetime import UTC, datetime, timedelta

import pytest

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.logs import ExcerptConfig, build_excerpts
from cicd_doctor.core.models import FailedJob, JobRef, RawLog
from tests.factories import make_run_ref
from tests.logcases import case_excerpts, load_log_case, log_case_names

COUNTER = TiktokenCounter()
T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def test_counts_tokens_with_o200k_base() -> None:
    assert COUNTER.count("") == 0
    assert 1 <= COUNTER.count("ModuleNotFoundError: No module named 'requests'") < 20
    # special-token text in a log is counted as ordinary text, not rejected
    assert COUNTER.count("<|endoftext|>") > 1


@pytest.mark.parametrize("name", log_case_names())
def test_fixture_excerpts_stay_within_6000_tokens(name: str) -> None:
    excerpts = case_excerpts(load_log_case(name), COUNTER)

    assert sum(excerpt.token_count for excerpt in excerpts) <= 6000


def test_three_huge_jobs_share_6000_real_tokens() -> None:
    def job(job_id: int) -> FailedJob:
        return FailedJob(
            job=JobRef(run=make_run_ref(), job_id=job_id, job_name=f"job {job_id}"),
            conclusion="failure",
            step_name=None,
            step_number=None,
            started_at=T0,
            completed_at=T0 + timedelta(minutes=5),
            runner_os="Linux",
        )

    stamp = "2026-11-13T10:00:05.0000000Z"
    lines = [
        f"{stamp} E   AssertionError: expected payload {n} to match schema v{n % 7}"
        if n % 40 == 0
        else f"{stamp} tests/test_api.py::test_case_{n} PASSED [{n % 100:3d}%]"
        for n in range(20_000)
    ]
    raw = RawLog(text="\n".join(lines) + "\n", truncated=False)

    excerpts = build_excerpts([(job(1), raw), (job(2), raw), (job(3), raw)], COUNTER)

    tokens = [excerpt.token_count for excerpt in excerpts]
    assert sum(tokens) <= ExcerptConfig().max_tokens
    assert min(tokens) >= 1900  # the budget is split evenly
    for excerpt in excerpts:
        recount = sum(COUNTER.count(f"L{line.number}: {line.text}\n") for line in excerpt.lines)
        assert recount == excerpt.token_count
