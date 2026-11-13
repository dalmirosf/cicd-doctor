from datetime import timedelta

import pytest

from cicd_doctor.core.retry import (
    BACKOFF_CAP,
    LAST_ERROR_MAX_BYTES,
    backoff_ceiling,
    backoff_delay,
    truncate_error,
)


def test_backoff_ceilings_follow_the_spec_and_stop_at_30_minutes() -> None:
    ceilings = [backoff_ceiling(attempt) for attempt in range(1, 7)]

    assert ceilings == [
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=8),
        timedelta(minutes=30),
        timedelta(minutes=30),
        timedelta(minutes=30),
    ]


def test_huge_attempt_numbers_stay_at_the_cap() -> None:
    assert backoff_ceiling(10_000) == BACKOFF_CAP


@pytest.mark.parametrize("jitter", [0.0, 0.25, 1.0])
def test_full_jitter_scales_the_ceiling(jitter: float) -> None:
    assert backoff_delay(2, jitter) == timedelta(minutes=2) * jitter


@pytest.mark.parametrize("jitter", [-0.1, 1.1])
def test_jitter_outside_zero_one_is_rejected(jitter: float) -> None:
    with pytest.raises(ValueError, match="jitter"):
        backoff_delay(1, jitter)


def test_attempts_start_at_one() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_ceiling(0)


def test_short_errors_are_kept_as_is() -> None:
    assert truncate_error("TransientError: 502") == "TransientError: 502"


def test_long_errors_are_cut_on_a_character_boundary() -> None:
    text = "a" + "€" * 1000  # 1 + 3000 bytes

    cut = truncate_error(text)

    assert len(cut.encode("utf-8")) <= LAST_ERROR_MAX_BYTES
    assert text.startswith(cut)
    assert len(cut.encode("utf-8")) > LAST_ERROR_MAX_BYTES - 3
