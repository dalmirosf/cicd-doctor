"""Clean log lines, drop noise and flag error lines (SPEC §7.3 steps 3-5)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cicd_doctor.core.logs.errors import is_error_line
from cicd_doctor.core.logs.noise import NOISE_GROUPS, is_noise
from cicd_doctor.core.logs.parse import RawLine

# CSI sequences (colors, cursor moves), OSC sequences (titles, links) and other 2-byte escapes.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
GROUP_START = "##[group]"
GROUP_END = "##[endgroup]"
MIN_REPEATS = 3  # this many identical consecutive lines collapse into one


@dataclass(frozen=True, slots=True)
class CleanLine:
    number: int
    text: str
    is_error: bool


def clean_text(content: str) -> str:
    """Keep the final state of ``\\r`` progress updates and strip ANSI escapes."""
    if "\r" in content:
        updates = [part for part in content.split("\r") if part.strip()]
        content = updates[-1] if updates else ""
    if "\x1b" in content:
        content = _ANSI.sub("", content)
    return content.rstrip()


def clean_lines(
    lines: Sequence[RawLine], error_patterns: Sequence[re.Pattern[str]] = ()
) -> list[CleanLine]:
    """Clean ``lines`` and drop noise. Error lines are always kept, even inside noise groups."""
    cleaned: list[CleanLine] = []
    in_noise_group = False
    for line in lines:
        text = clean_text(line.content)
        if text.startswith(GROUP_END):
            in_noise_group = False
            continue
        if text.startswith(GROUP_START):
            title = text.removeprefix(GROUP_START).strip()
            in_noise_group = title in NOISE_GROUPS
            if in_noise_group:
                continue
            text = title  # the group title is kept as context (§7.3)
        is_error = is_error_line(text, error_patterns)
        if not is_error and (in_noise_group or is_noise(text)):
            continue
        cleaned.append(CleanLine(line.number, text, is_error))
    return collapse_repeats(cleaned)


def collapse_repeats(lines: Sequence[CleanLine]) -> list[CleanLine]:
    """Collapse ``MIN_REPEATS`` or more identical consecutive lines into the first one plus
    ``(repeated N times)``, and runs of blank lines into one blank line."""
    result: list[CleanLine] = []
    index = 0
    while index < len(lines):
        first = lines[index]
        end = index + 1
        while end < len(lines) and lines[end].text == first.text:
            end += 1
        run = end - index
        if not first.text.strip():
            result.append(first)
        elif run >= MIN_REPEATS:
            text = f"{first.text} (repeated {run} times)"
            result.append(CleanLine(first.number, text, first.is_error))
        else:
            result.extend(lines[index:end])
        index = end
    return result
