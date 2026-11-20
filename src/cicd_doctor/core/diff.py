"""The diff of a pull request as the model sees it (SPEC §9.3, D-97).

``parse_patch`` turns the ``patch`` of one file into hunks of NEW-side lines (the lines a review
comment can anchor to). ``select_for_prompt`` keeps the whole hunks of the files that the log
names, in a bounded number of lines and tokens. Everything here is pure and treats the diff as
untrusted: a malformed patch gives no hunks, a path with control characters is never used.
"""

import re
from collections.abc import Sequence

from cicd_doctor.core.logs.clean import strip_terminal_controls
from cicd_doctor.core.models import (
    DiffHunk,
    DiffLine,
    LogExcerpt,
    PromptDiff,
    PullDiff,
    ShownFile,
)
from cicd_doctor.core.ports import TokenCounter

MAX_FILES = 5  # files of the diff shown to the model
MAX_LINES = 160  # lines shown, all files together
MAX_TOKENS = 2000  # real tokens (o200k_base) of the block: documentation lines are very long
MAX_LINE_CHARS = 200  # one line of the diff is cut to this in the prompt
MAX_PATH_CHARS = 260
REMOVED = "removed"

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PATH_BOUNDARY = r"(?<![\w.\-])"


def parse_patch(patch: str | None) -> list[DiffHunk]:
    """The hunks of one ``patch``, with the numbers and texts of their new side.

    A real ``patch`` has no trailing newline and may contain ``\\ No newline at end of file``
    lines, which are not lines of the file. Each hunk is read by the counts of its header, so a
    line that starts like a header inside a hunk is just a line. A patch that does not follow its
    headers (a wrong count, an unknown prefix, text between hunks) gives **no** hunks at all:
    a suggestion is never placed from a diff that could not be read in full.
    """
    if not patch:
        return []
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hunks: list[DiffHunk] = []
    index = 0
    while index < len(lines):
        header = _HEADER.match(lines[index])
        if header is None:
            return []
        old_left = int(header.group(2) if header.group(2) is not None else 1)
        new_left = int(header.group(4) if header.group(4) is not None else 1)
        number = int(header.group(3))
        index += 1
        kept: list[DiffLine] = []
        while old_left > 0 or new_left > 0:
            if index >= len(lines):
                return []
            line = lines[index]
            index += 1
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            marker, text = (line[:1], line[1:]) if line else (" ", "")
            if marker == " ":
                old_left, new_left = old_left - 1, new_left - 1
                kept.append(DiffLine(number=number, text=text, added=False))
                number += 1
            elif marker == "+":
                new_left -= 1
                kept.append(DiffLine(number=number, text=text, added=True))
                number += 1
            elif marker == "-":
                old_left -= 1
            else:
                return []
            if old_left < 0 or new_left < 0:
                return []
        while index < len(lines) and lines[index].startswith("\\"):
            index += 1
        if kept:
            hunks.append(DiffHunk(lines=kept))
    return hunks


def usable_path(path: str) -> bool:
    """A file path that may be shown to the model and named by a suggestion: not empty, not too
    long, no control characters (a newline in a path could forge a line of the prompt)."""
    return bool(path) and len(path) <= MAX_PATH_CHARS and _CONTROL.search(path) is None


def select_for_prompt(
    diff: PullDiff | None,
    excerpt: LogExcerpt,
    counter: TokenCounter,
    *,
    max_files: int = MAX_FILES,
    max_lines: int = MAX_LINES,
    max_tokens: int = MAX_TOKENS,
) -> PromptDiff | None:
    """The hunks of the files that the log excerpt names, or ``None`` when there is nothing to show.

    A truncated diff gives ``None`` (the files left out could change the picture). A file counts
    when its current path appears in an excerpt line; removed files and files with no ``patch`` are
    skipped. The hunks of a file that the log points at with ``path:line`` come first. Only whole
    hunks are shown, so what the model saw is exactly what a suggestion may touch; a hunk that does
    not fit in the line or token bounds is left out and a smaller one after it may still fit.
    """
    if diff is None or diff.truncated:
        return None
    texts = [line.text for line in excerpt.lines]
    shown: list[ShownFile] = []
    used_lines = 0
    rendered: list[str] = []
    for file in diff.files:
        if len(shown) >= max_files:
            break
        if file.status == REMOVED or file.patch is None or not usable_path(file.filename):
            continue
        if not _named(file.filename, texts):
            continue
        pointed = _pointed_lines(file.filename, texts)
        hunks = sorted(
            parse_patch(file.patch),
            key=lambda hunk: not any(hunk.first <= n <= hunk.last for n in pointed),
        )
        kept: list[DiffHunk] = []
        for hunk in hunks:
            block = [render_diff_line(line) for line in hunk.lines]
            if used_lines + len(block) + 1 > max_lines:
                continue
            candidate = [*rendered, file.filename, *block]
            if counter.count("\n".join(candidate)) > max_tokens:
                continue
            kept.append(hunk)
            rendered = candidate
            used_lines += len(block) + 1
        if kept:
            kept.sort(key=lambda hunk: hunk.first)
            shown.append(ShownFile(path=file.filename, status=file.status, hunks=kept))
    if not shown:
        return None
    return PromptDiff(files=shown, token_count=counter.count("\n".join(rendered)))


def render_diff_line(line: DiffLine) -> str:
    """``12: context`` or ``13+ added``: the number to copy, then the text (cut, no controls)."""
    text = strip_terminal_controls(line.text).replace("\t", "    ")
    text = _CONTROL.sub("", text)[:MAX_LINE_CHARS]
    return f"{line.number}{'+' if line.added else ':'} {text}"


def _named(path: str, texts: Sequence[str]) -> bool:
    pattern = re.compile(_PATH_BOUNDARY + re.escape(path) + r"(?![\w\-])")
    return any(pattern.search(text) for text in texts)


def _pointed_lines(path: str, texts: Sequence[str]) -> set[int]:
    """Line numbers the log gives for this file: ``path:12``, ``path(12)``, ``"path", line 12``."""
    pattern = re.compile(_PATH_BOUNDARY + re.escape(path) + r"[\"']?(?::|\(|, line )\s*(\d{1,7})")
    return {int(match.group(1)) for text in texts for match in pattern.finditer(text)}
