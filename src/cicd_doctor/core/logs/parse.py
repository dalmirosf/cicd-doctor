"""Split a GitHub job log into numbered lines and slice the failed step (SPEC §7.3 steps 1-2).

Every line of a job log starts with an ISO-8601 UTC timestamp with 7 fractional digits
(``2026-11-13T10:00:05.1234567Z ``). A line without one inherits the previous timestamp.
Timestamps are kept as ``YYYY-MM-DDTHH:MM:SS`` strings: they compare correctly as text and
the slice only needs second precision (it allows 1 s of tolerance anyway).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z ?")
SLICE_TOLERANCE = timedelta(seconds=1)
ERROR_MARKER = "##[error]"


@dataclass(frozen=True, slots=True)
class RawLine:
    number: int  # 1-based, in the retained log
    second: str | None  # timestamp truncated to the second, or None before the first one
    content: str  # the line without its timestamp


def parse_log(text: str) -> list[RawLine]:
    """Number the lines of ``text`` and take their timestamps off."""
    text = text.removeprefix("﻿")
    # split("\n"), not splitlines(): a bare "\r" is a progress update inside one line (§7.3).
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    lines: list[RawLine] = []
    second: str | None = None
    for number, part in enumerate(parts, start=1):
        part = part.removesuffix("\r")
        match = _TIMESTAMP.match(part)
        if match:
            second = match.group(1)
            part = part[match.end() :]
        lines.append(RawLine(number, second, part))
    return lines


def slice_step(
    lines: Sequence[RawLine], started_at: datetime | None, completed_at: datetime | None
) -> list[RawLine]:
    """The lines of the failed step plus every ``##[error]`` line of the job.

    Without step times, or when no line falls inside them (clock skew, a log cut before the
    step), the whole job is used.
    """
    if started_at is None or completed_at is None:
        return list(lines)
    low = _second(started_at - SLICE_TOLERANCE)
    high = _second(completed_at + SLICE_TOLERANCE)
    sliced: list[RawLine] = []
    found_step = False
    for line in lines:
        if line.second is not None and low <= line.second <= high:
            sliced.append(line)
            found_step = True
        elif ERROR_MARKER in line.content:
            sliced.append(line)
    return sliced if found_step else list(lines)


def _second(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
