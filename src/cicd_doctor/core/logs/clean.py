"""Clean log lines, drop noise and flag error lines (SPEC §7.3 steps 3-5)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cicd_doctor.core.logs.errors import is_error_line
from cicd_doctor.core.logs.noise import NOISE_GROUPS, is_noise
from cicd_doctor.core.logs.parse import RawLine

# CSI sequences (colors, cursor moves), OSC sequences (titles, links) and the other 2-byte
# escapes (ESC followed by 0x30-0x7E: ESC 7 / ESC 8 save and restore the cursor, ESC M scrolls...).
# An OSC body cannot contain a newline, so an unterminated one never swallows the next lines
# when the pattern runs over a whole log (D-62); CSI and the 2-byte forms cannot either.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\n]*(?:\x07|\x1b\\)|[0-Z\\-~])")
# What is left of the control characters once the sequences are gone: C0 except TAB, LF and CR
# (ESC included), DEL, and the C1 range (U+009B is a one-character CSI on some terminals).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
GROUP_START = "##[group]"
GROUP_END = "##[endgroup]"
MIN_REPEATS = 3  # this many identical consecutive lines collapse into one


@dataclass(frozen=True, slots=True)
class CleanLine:
    number: int
    text: str
    is_error: bool


def strip_terminal_controls(text: str) -> str:
    """Remove terminal escape sequences and other control characters from a whole log (D-62).

    Used before a log is stored (``python -m evals capture``), so a stored log can never move a
    cursor, set a window title or rewrite the screen when someone prints it. TAB, LF and CR stay:
    the excerpt needs them (a bare CR is a progress update, SPEC §7.3). No newline is ever
    removed, so line numbers do not move. Idempotent: the result has no ESC, so a second pass
    finds nothing to remove.
    """
    return _CONTROL.sub("", _ANSI.sub("", text))


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
