"""Parsing, step slicing, cleaning, noise and error detection (SPEC §7.3 steps 1-5)."""

import re
from datetime import UTC, datetime

import pytest

from cicd_doctor.core.logs.clean import CleanLine, clean_lines, clean_text, collapse_repeats
from cicd_doctor.core.logs.errors import MAX_SCAN_CHARS, is_error_line
from cicd_doctor.core.logs.noise import is_noise
from cicd_doctor.core.logs.parse import RawLine, parse_log, slice_step


def _raw(*contents: str) -> list[RawLine]:
    return [RawLine(number, None, text) for number, text in enumerate(contents, start=1)]


def _texts(lines: list[CleanLine]) -> list[str]:
    return [line.text for line in lines]


# --- parse ------------------------------------------------------------------------------------


def test_parse_numbers_lines_and_strips_timestamps() -> None:
    text = (
        "﻿2026-11-13T10:00:05.1234567Z first\r\n"
        "continuation without timestamp\n"
        "2026-11-13T10:00:06.0000000Z third\n"
    )

    lines = parse_log(text)

    assert lines == [
        RawLine(1, "2026-11-13T10:00:05", "first"),
        RawLine(2, "2026-11-13T10:00:05", "continuation without timestamp"),
        RawLine(3, "2026-11-13T10:00:06", "third"),
    ]


def test_parse_keeps_carriage_returns_inside_a_line() -> None:
    lines = parse_log("2026-11-13T10:00:05.1Z 10%\r50%\r100%\nnext")

    assert [line.content for line in lines] == ["10%\r50%\r100%", "next"]


def test_parse_of_empty_text() -> None:
    assert parse_log("") == []


# --- slice ------------------------------------------------------------------------------------


LOG = [
    RawLine(1, None, "before any timestamp"),
    RawLine(2, "2026-11-13T10:00:01", "set up"),
    RawLine(3, "2026-11-13T10:00:01", "##[error]an error in another step"),
    RawLine(4, "2026-11-13T10:00:04", "step line, inside the 1 s tolerance"),
    RawLine(5, "2026-11-13T10:00:05", "step line"),
    RawLine(6, "2026-11-13T10:00:09", "step line"),
    RawLine(7, "2026-11-13T10:00:10", "after, inside the tolerance"),
    RawLine(8, "2026-11-13T10:00:11", "after"),
]


def test_slice_keeps_the_step_with_tolerance_and_every_error_line() -> None:
    sliced = slice_step(
        LOG,
        datetime(2026, 11, 13, 10, 0, 5, tzinfo=UTC),
        datetime(2026, 11, 13, 10, 0, 9, 500000, tzinfo=UTC),
    )

    assert [line.number for line in sliced] == [3, 4, 5, 6, 7]


def test_slice_accepts_naive_times_as_utc() -> None:
    sliced = slice_step(LOG, datetime(2026, 11, 13, 10, 0, 5), datetime(2026, 11, 13, 10, 0, 9))

    assert [line.number for line in sliced] == [3, 4, 5, 6, 7]


def test_slice_without_step_times_or_matching_lines_uses_the_whole_job() -> None:
    later = datetime(2026, 11, 14, tzinfo=UTC)

    assert slice_step(LOG, None, None) == LOG
    assert slice_step(LOG, later, later) == LOG


# --- clean ------------------------------------------------------------------------------------


def test_clean_text_keeps_the_last_progress_update_and_strips_ansi() -> None:
    assert clean_text("10%\r50%\r100% done\r") == "100% done"
    assert clean_text("\x1b[1m\x1b[31mFAILED\x1b[0m test_x   ") == "FAILED test_x"
    assert clean_text("\x1b]8;;https://x\x07link\x1b]8;;\x07") == "link"
    assert clean_text("\r\r") == ""


def test_group_titles_are_kept_and_end_markers_dropped() -> None:
    lines = clean_lines(_raw("##[group]Run pytest", "pytest -q", "##[endgroup]", "1 passed"))

    assert _texts(lines) == ["Run pytest", "pytest -q", "1 passed"]
    assert [line.number for line in lines] == [1, 2, 4]


def test_boilerplate_groups_are_dropped_but_their_errors_are_kept() -> None:
    lines = clean_lines(
        _raw(
            "##[group]Fetching the repository",
            "[command]/usr/bin/git fetch origin",
            "##[error]fatal: repository 'https://github.com/acme/missing/' not found",
            "##[endgroup]",
            "##[group]Operating System",
            "Ubuntu",
            "##[endgroup]",
            "after",
        )
    )

    assert _texts(lines) == [
        "##[error]fatal: repository 'https://github.com/acme/missing/' not found",
        "after",
    ]
    assert lines[0].is_error


def test_noise_is_dropped_unless_it_is_an_error() -> None:
    lines = clean_lines(
        _raw(
            "Requirement already satisfied: pip in /usr/lib/python3",
            "Collecting requests",
            "Downloading ERROR-report.whl",  # noise and error at once: kept
            "real output",
        )
    )

    assert _texts(lines) == ["Downloading ERROR-report.whl", "real output"]


def test_extra_error_patterns_flag_more_lines() -> None:
    raw = _raw("src/app.py:3: error: Incompatible types [assignment]")

    assert not clean_lines(raw)[0].is_error
    assert clean_lines(raw, [re.compile(r"error: .* \[[a-z-]+\]")])[0].is_error


def test_three_identical_lines_collapse_two_do_not() -> None:
    lines = [CleanLine(n, "retrying", False) for n in range(1, 5)]
    lines += [CleanLine(5, "x", False), CleanLine(6, "x", False)]
    lines += [CleanLine(7, "", False), CleanLine(8, "", False), CleanLine(9, "end", True)]

    collapsed = collapse_repeats(lines)

    assert [(line.number, line.text) for line in collapsed] == [
        (1, "retrying (repeated 4 times)"),
        (5, "x"),
        (6, "x"),
        (7, ""),
        (9, "end"),
    ]
    assert collapsed[-1].is_error


def test_a_repeated_error_line_stays_an_error() -> None:
    collapsed = collapse_repeats([CleanLine(n, "ERROR boom", True) for n in range(3)])

    assert collapsed == [CleanLine(0, "ERROR boom (repeated 3 times)", True)]


# --- noise and error patterns -----------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Requirement already satisfied: setuptools in ./.venv/lib/python3.13/site-packages",
        "Downloading requests-2.32.3-py3-none-any.whl (64 kB)",
        "Collecting urllib3<3,>=1.21.1",
        "Using cached idna-3.10-py3-none-any.whl",
        "Resolved 14 packages in 2ms",
        "Installed 9 packages in 18ms",
        " + pytest==8.4.2",
        "added 312 packages, and audited 313 packages in 4s",
        "a2318d6c47ec: Pulling fs layer",
        "a2318d6c47ec: Download complete",
        "#4 sha256:8a1e25ce7c4f75e372e9884f8f7b1bedcfe4a7a7d452eb4b0a1c7477c9a90345 3.15MB / 29.13MB 0.1s",  # noqa: E501
        "   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 64.9/64.9 kB 2.1 MB/s eta 0:00:00",
        "[=========================>                         ]  14.5MB/29.1MB",
        "Current runner version: '2.329.0'",
        "Runner Image Provisioner",
        "Prepare workflow directory",
        "Complete job name: test",
        "Post job cleanup.",
        "[command]/usr/bin/git config --local --name-only --get-regexp core\\.sshCommand",
        "Cleaning up orphan processes",
    ],
)
def test_noise_lines(line: str) -> None:
    assert is_noise(line)


@pytest.mark.parametrize(
    "line",
    [
        "FAILED tests/test_math.py::test_add - assert 3 == 4",
        "Downloaded 3 files",
        "Collected 12 items",
        "Run uv sync --locked",
        "[command]/usr/bin/git checkout --progress --force -B main",
        "Installing collected packages: requests",
        "added a new test",
    ],
)
def test_not_noise_lines(line: str) -> None:
    assert not is_noise(line)


@pytest.mark.parametrize(
    "line",
    [
        "##[error]Process completed with exit code 1.",
        "ModuleNotFoundError: No module named 'requests'",
        "ERROR collecting tests/test_x.py",
        "FAILED tests/test_x.py::test_y",
        "FAIL: test_parse (tests.test_parser.ParserTest)",
        "Traceback (most recent call last):",
        "java.lang.IllegalStateException",
        "fatal: not a git repository",
        "panic: runtime error: index out of range",
        "exit status 2",
        "Killed",
        "Error: Process completed with exit code 137.",
    ],
)
def test_error_lines(line: str) -> None:
    assert is_error_line(line)


@pytest.mark.parametrize(
    "line",
    ["1 passed in 0.1s", "errors = 0", "Failed to fetch optional metadata, continuing", "error"],
)
def test_not_error_lines(line: str) -> None:
    assert not is_error_line(line)


def test_error_search_is_capped() -> None:
    assert not is_error_line("x" * MAX_SCAN_CHARS + " Traceback")
    assert is_error_line("x" * (MAX_SCAN_CHARS - 20) + " Traceback")
