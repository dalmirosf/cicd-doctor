"""Check a suggested change against the diff the model was shown (SPEC §5.1 rule 3, D-97).

A suggestion becomes a review comment with a ``suggestion`` block that one click commits. So it
is kept only when it can be applied exactly where it says: the path is a file of the diff (its
current name), every line of the range is a line of the NEW side of ONE hunk the model was shown,
the range is at most 30 lines, and the replacement is clean text that was not altered by the
redaction. Anything else is dropped and ``suggested_fix`` stays as plain text (D-19).
"""

import re

from cicd_doctor.core.models import PromptDiff, Suggestion
from cicd_doctor.core.redaction import redact

MAX_SUGGESTION_LINES = 30  # SPEC §5.1 rule 3
MAX_REPLACEMENT_CHARS = 2000
MAX_PATH_CHARS = 260

_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Controls that cannot be part of source text (a tab and the line breaks can).
_BAD_REPLACEMENT_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def suggestion_marker(pr_number: int, failure_id: object) -> str:
    """The hidden first line of the review comment of one failure (the reporter finds it again).
    ``failure_id`` is a UUID built by us; the format is fixed, never text from a log."""
    return f"<!-- cicd-doctor:v1 suggestion pr={int(pr_number)} failure={failure_id} -->"


def safe_path(path: str) -> bool:
    """A path relative to the repository root that stays inside it."""
    segments = re.split(r"[\/]", path)
    return not (
        not path
        or len(path) > MAX_PATH_CHARS
        or path.startswith(("/", "\\", "~"))
        or _DRIVE.match(path) is not None
        or ".." in segments
        or "" in segments
        or "." in segments
        or _CONTROL.search(path) is not None
    )


def fits_diff(path: str, start_line: int, end_line: int, diff: PromptDiff | None) -> bool:
    """Whether ``start_line..end_line`` of ``path`` lies inside ONE hunk the model was shown.

    ``path`` must be the exact current name of a shown file: a renamed file is addressed by its
    new name, and a path with ``..``, an absolute path or another spelling of the same file does
    not match. A range that spans two hunks is refused even when both are shown.
    """
    if diff is None or not safe_path(path):
        return False
    span = end_line - start_line + 1
    if start_line < 1 or not 1 <= span <= MAX_SUGGESTION_LINES:
        return False
    return any(
        hunk.first <= start_line and end_line <= hunk.last
        for file in diff.files
        if file.path == path
        for hunk in file.hunks
    )


def clean_replacement(text: str) -> str | None:
    """The replacement as it will be committed, or ``None`` when it cannot be used.

    Line breaks are normalised and a trailing one dropped (the block adds its own). An empty
    replacement (it would delete lines), control characters, a text over the limit and a text that
    the redaction would change (a secret in code that one click would commit as ``[REDACTED]``) are
    all refused.
    """
    normal = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if (
        not normal.strip()
        or len(normal) > MAX_REPLACEMENT_CHARS
        or _BAD_REPLACEMENT_CHARS.search(normal) is not None
        or redact(normal) != normal
    ):
        return None
    return normal


def checked_against_diff(
    suggestion: Suggestion | None, diff: PromptDiff | None
) -> Suggestion | None:
    """The suggestion if it passes every check of the module, else ``None``."""
    if suggestion is None:
        return None
    replacement = clean_replacement(suggestion.replacement)
    path = suggestion.path.strip()
    if replacement is None or not fits_diff(path, suggestion.start_line, suggestion.end_line, diff):
        return None
    return Suggestion(
        path=path,
        start_line=suggestion.start_line,
        end_line=suggestion.end_line,
        replacement=replacement,
    )
