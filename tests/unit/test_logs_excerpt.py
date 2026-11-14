"""The excerpt: windows, token budget, trimming order and redaction (SPEC §7.3 steps 6-8)."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.logs import ExcerptConfig, build_excerpt, build_excerpts, share_budget
from cicd_doctor.core.logs.excerpt import TRUNCATED_LINE_SUFFIX, render_line
from cicd_doctor.core.models import FailedJob, JobRef, LogExcerpt, LogLine, RawLog
from cicd_doctor.core.redaction import placeholder
from tests.factories import make_run_ref
from tests.redaction_corpus import planted_pem_block, planted_secrets

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
COUNTER = CharTokenCounter()


class OneTokenPerLine:
    """Every rendered line costs exactly one token: makes budgets easy to reason about."""

    def count(self, text: str) -> int:
        return 1


def _job(job_id: int = 1, step: bool = False) -> FailedJob:
    return FailedJob(
        job=JobRef(run=make_run_ref(), job_id=job_id, job_name=f"job {job_id}"),
        conclusion="failure",
        step_name="Run tests" if step else None,
        step_number=3 if step else None,
        started_at=T0,
        completed_at=T0 + timedelta(minutes=1),
        runner_os="Linux",
        step_started_at=T0 + timedelta(seconds=10) if step else None,
        step_completed_at=T0 + timedelta(seconds=20) if step else None,
    )


def _log(lines: list[str], second: int = 15) -> RawLog:
    stamp = (T0 + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    return RawLog(text="".join(f"{stamp} {line}\n" for line in lines), truncated=False)


def _numbers(excerpt: LogExcerpt) -> list[int]:
    return [line.number for line in excerpt.lines]


def test_short_log_is_kept_whole() -> None:
    excerpt = build_excerpt(_job(), _log(["a", "b", "ERROR c"]), OneTokenPerLine())

    assert _numbers(excerpt) == [1, 2, 3]
    assert excerpt.error_lines == [3]
    assert excerpt.omitted == []
    assert excerpt.token_count == 3
    assert excerpt.truncated is False
    assert excerpt.redactions == 0
    assert excerpt.step_name is None
    assert excerpt.job.job_id == 1


def test_window_is_the_last_lines_plus_context_around_each_error() -> None:
    lines = [f"line {n}" for n in range(1, 1001)]
    lines[99] = "ERROR early failure"  # line 100
    config = ExcerptConfig(tail_lines=10, error_context=2, protected_context=1)

    excerpt = build_excerpt(_job(), _log(lines), OneTokenPerLine(), config)

    assert _numbers(excerpt) == [98, 99, 100, 101, 102, *range(991, 1001)]
    assert excerpt.omitted == [(1, 97), (103, 990)]
    assert excerpt.error_lines == [100]


def test_trimming_cuts_the_oldest_context_first_and_errors_last() -> None:
    lines = [f"line {n}" for n in range(1, 301)]
    lines[49] = "ERROR first"  # 50
    lines[249] = "ERROR second"  # 250
    config = ExcerptConfig(max_tokens=20, tail_lines=30, error_context=5, protected_context=1)

    excerpt = build_excerpt(_job(), _log(lines), OneTokenPerLine(), config)

    # 6 protected lines (49-51, 249-251), then the newest context lines.
    assert _numbers(excerpt) == [49, 50, 51, 249, 250, 251, *range(287, 301)]
    assert excerpt.token_count == 20


def test_when_errors_alone_exceed_the_budget_the_newest_are_kept() -> None:
    lines = [f"ERROR number {n}" for n in range(1, 51)]
    config = ExcerptConfig(max_tokens=5, tail_lines=0, error_context=0, protected_context=0)

    excerpt = build_excerpt(_job(), _log(lines), OneTokenPerLine(), config)

    assert _numbers(excerpt) == [46, 47, 48, 49, 50]


def test_the_failed_step_is_sliced_by_time() -> None:
    before = _log(["setup noise line", "ERROR in setup"], second=2).text
    inside = _log(["step output", "ERROR in the step"], second=15).text
    after = _log(["cleanup output"], second=40).text
    raw = RawLog(text=before + inside + after, truncated=False)

    excerpt = build_excerpt(_job(step=True), raw, OneTokenPerLine())

    assert [line.text for line in excerpt.lines] == ["step output", "ERROR in the step"]
    assert excerpt.step_name == "Run tests"
    assert excerpt.omitted == [(1, 2)]


def test_the_budget_is_shared_between_jobs() -> None:
    long_lines = [f"line {n}" for n in range(200)]
    logs = [
        (_job(1), _log(["ERROR a"])),  # needs 1 token
        (_job(2), _log(long_lines)),
        (_job(3), _log(long_lines)),
    ]
    config = ExcerptConfig(max_tokens=101, tail_lines=150)

    excerpts = build_excerpts(logs, OneTokenPerLine(), config)

    assert [excerpt.token_count for excerpt in excerpts] == [1, 50, 50]
    assert sum(excerpt.token_count for excerpt in excerpts) <= config.max_tokens


@pytest.mark.parametrize(
    ("needs", "total", "expected"),
    [
        ([10, 20], 100, [10, 20]),
        ([100, 100], 100, [50, 50]),
        ([10, 100, 100], 100, [10, 45, 45]),
        ([100, 100, 100], 100, [33, 33, 34]),
        ([], 100, []),
    ],
)
def test_share_budget(needs: list[int], total: int, expected: list[int]) -> None:
    assert share_budget(needs, total) == expected


@settings(max_examples=200, deadline=None)
@given(needs=st.lists(st.integers(0, 10_000), max_size=5), total=st.integers(1, 20_000))
def test_share_budget_never_exceeds_the_total_or_a_need(needs: list[int], total: int) -> None:
    budgets = share_budget(needs, total)

    assert sum(budgets) <= total
    assert all(0 <= budget <= need for budget, need in zip(budgets, needs, strict=True))
    if sum(needs) > total:
        assert sum(budgets) == total  # nothing is wasted


def test_planted_secrets_never_reach_the_excerpt() -> None:
    planted = planted_secrets()
    lines = [
        "Run deploy",
        *[p.line for p in planted],
        "##[error]Process completed with exit code 1.",
    ]

    excerpt = build_excerpt(_job(), _log(lines), COUNTER)

    text = "\n".join(line.text for line in excerpt.lines)
    assert not [p.kind for p in planted if p.secret in text]
    assert excerpt.redactions >= len(planted)


def test_a_pem_key_cut_by_the_window_is_still_redacted() -> None:
    pem, body = planted_pem_block()
    lines = ["start", *pem, *[f"line {n}" for n in range(40)], "ERROR boom"]
    # The window starts in the middle of the key: its BEGIN line is outside.
    config = ExcerptConfig(tail_lines=len(lines) - 3, error_context=0)

    excerpt = build_excerpt(_job(), _log(lines), OneTokenPerLine(), config)

    texts = [line.text for line in excerpt.lines]
    assert "-----BEGIN RSA PRIVATE KEY-----" not in texts
    assert not any(secret in text for secret in body for text in texts)
    assert placeholder("private_key") in texts


def test_long_lines_are_cut_after_redaction() -> None:
    token = planted_secrets()[0].secret
    line = "x" * 1990 + " " + token  # the secret straddles the 2,000-char cut
    config = ExcerptConfig(max_line_chars=2000)

    excerpt = build_excerpt(_job(), _log([line]), COUNTER, config)

    [kept] = excerpt.lines
    assert kept.text.endswith(TRUNCATED_LINE_SUFFIX)
    assert token[:6] not in kept.text


def test_token_count_is_the_sum_of_the_rendered_lines() -> None:
    excerpt = build_excerpt(_job(), _log(["alpha", "ERROR beta"]), COUNTER)

    expected = sum(COUNTER.count(render_line(line) + "\n") for line in excerpt.lines)
    assert excerpt.token_count == expected
    assert render_line(LogLine(number=7, text="x")) == "L7: x"


def test_huge_step_stays_within_the_budget() -> None:
    lines = [f"compiling module {n} of 50000" for n in range(50_000)]
    for n in range(0, 50_000, 500):
        lines[n] = f"ERROR: warning promoted to error in module {n}"

    excerpt = build_excerpt(_job(), _log(lines), COUNTER)

    assert 0 < excerpt.token_count <= 6000
    assert excerpt.error_lines[-1] == 49_501  # the newest error survives; older context goes


def test_truncated_raw_logs_stay_truncated() -> None:
    raw = _log(["ERROR x"]).model_copy(update={"truncated": True})

    assert build_excerpt(_job(), raw, COUNTER).truncated is True


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tokens": 0},
        {"max_bytes": 0},
        {"max_line_chars": 0},
        {"tail_lines": -1},
        {"error_context": -1},
        {"protected_context": -1},
    ],
)
def test_invalid_config_is_rejected(changes: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        ExcerptConfig(**changes)
