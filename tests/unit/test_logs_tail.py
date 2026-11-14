"""Tail-biased retention of job logs (FR-07, D-11, D-58), including the 50 MB acceptance case."""

import time

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.logs import ExcerptConfig, TailBuffer, build_excerpt, tail_text
from cicd_doctor.core.logs.tail import DEFAULT_MAX_BYTES, HARD_READ_LIMIT
from cicd_doctor.core.models import RawLog
from tests.logcases import load_log_case, select_case_jobs

MB = 1024 * 1024


def _feed(buffer: TailBuffer, data: bytes, chunk: int) -> int:
    fed = 0
    for start in range(0, len(data), chunk):
        fed += 1
        if not buffer.feed(data[start : start + chunk]):
            break
    return fed


def test_a_log_under_the_limit_is_kept_whole() -> None:
    buffer = TailBuffer(max_bytes=100, hard_limit=1000)
    _feed(buffer, b"one\ntwo\nthree\n", chunk=4)

    result = buffer.result()

    assert result.text == "one\ntwo\nthree\n"
    assert result.truncated is False


def test_only_the_tail_is_kept_from_the_first_complete_line() -> None:
    data = b"".join(f"line {i:03d}\n".encode() for i in range(100))  # 9 bytes per line
    buffer = TailBuffer(max_bytes=50, hard_limit=10_000)
    _feed(buffer, data, chunk=7)

    result = buffer.result()

    assert result.truncated is True
    assert result.text == "line 095\nline 096\nline 097\nline 098\nline 099\n"


def test_the_hard_read_limit_stops_reading() -> None:
    buffer = TailBuffer(max_bytes=10, hard_limit=25)

    assert buffer.feed(b"0123456789\n") is True
    assert buffer.feed(b"abcdefghij\nklmnop") is False  # crosses 25 bytes
    assert buffer.feed(b"more") is False
    assert buffer.bytes_read == 25
    assert buffer.result().truncated is True


def test_a_multibyte_character_cut_at_the_boundary_does_not_break_decoding() -> None:
    buffer = TailBuffer(max_bytes=7, hard_limit=100)
    buffer.feed("ñandú\nañejo\n".encode())

    result = buffer.result()

    assert result.text == "añejo\n"


def test_a_tail_without_newline_is_kept_as_one_line() -> None:
    buffer = TailBuffer(max_bytes=5, hard_limit=100)
    buffer.feed(b"x" * 20)

    assert buffer.result().text == "xxxxx"


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        TailBuffer(max_bytes=0)
    with pytest.raises(ValueError, match="max_bytes"):
        TailBuffer(max_bytes=10, hard_limit=5)


def test_tail_text_caps_text_already_in_memory() -> None:
    text = "".join(f"line {i}\n" for i in range(1000))

    kept, truncated = tail_text(text, max_bytes=100)

    assert truncated is True
    assert len(kept.encode()) <= 100
    assert kept.endswith("line 999\n")
    assert kept.startswith("line ")  # starts at a full line
    assert tail_text("short", max_bytes=100) == ("short", False)
    assert tail_text("é" * 30, max_bytes=100) == ("é" * 30, False)  # 60 bytes


def _synthetic_log(size: int) -> bytes:
    """``size`` bytes of timestamped log lines ending with an error."""
    line = b"2026-11-13T10:00:05.0000000Z building target 0123456789 abcdefghijklmnopqrstuvwxyz\n"
    error = (
        b"2026-11-13T10:00:06.0000000Z ModuleNotFoundError: No module named 'requests'\n"
        b"2026-11-13T10:00:06.1000000Z ##[error]Process completed with exit code 1.\n"
    )
    body = line * ((size - len(error)) // len(line))
    return body + error


def test_a_50_mb_log_keeps_only_the_last_20_mb_and_the_final_error() -> None:
    data = _synthetic_log(50 * MB)
    assert len(data) > 49 * MB
    buffer = TailBuffer()  # LOG_MAX_BYTES = 20 MB
    started = time.perf_counter()

    _feed(buffer, data, chunk=64 * 1024)
    raw = buffer.result()

    retained = raw.text.encode()
    assert raw.truncated is True
    assert DEFAULT_MAX_BYTES - 200 < len(retained) <= DEFAULT_MAX_BYTES
    assert data.endswith(retained)
    assert retained.endswith(b"##[error]Process completed with exit code 1.\n")

    job = select_case_jobs(load_log_case("py-modnf"))[0].model_copy(
        update={"step_started_at": None, "step_completed_at": None}
    )
    excerpt = build_excerpt(job, raw, CharTokenCounter())

    assert excerpt.truncated is True
    texts = [line.text for line in excerpt.lines]
    assert "ModuleNotFoundError: No module named 'requests'" in texts
    assert texts[-1] == "##[error]Process completed with exit code 1."
    assert excerpt.token_count <= ExcerptConfig().max_tokens
    assert time.perf_counter() - started < 20  # generous: the point is that it is bounded


def test_build_excerpt_caps_an_oversized_raw_log_itself() -> None:
    data = _synthetic_log(2 * MB).decode()
    job = select_case_jobs(load_log_case("py-modnf"))[0]
    config = ExcerptConfig(max_bytes=MB)

    excerpt = build_excerpt(job, RawLog(text=data, truncated=False), CharTokenCounter(), config)

    assert excerpt.truncated is True
    assert excerpt.lines[-1].text == "##[error]Process completed with exit code 1."


def test_the_hard_limit_default_is_100_mb() -> None:
    assert HARD_READ_LIMIT == 100 * MB
    assert DEFAULT_MAX_BYTES == 20 * MB
