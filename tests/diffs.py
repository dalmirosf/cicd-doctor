"""Builders of pull request diffs for the tests of suggestions (D-97)."""

from collections.abc import Sequence

from cicd_doctor.core.models import (
    DiffFile,
    DiffHunk,
    DiffLine,
    PromptDiff,
    PullDiff,
    ShownFile,
)


def hunk(first: int, last: int, *, added: Sequence[int] = ()) -> DiffHunk:
    """A hunk of new-side lines ``first..last``; the numbers in ``added`` are added lines."""
    return DiffHunk(
        lines=[
            DiffLine(number=n, text=f"line {n} of the file", added=n in added)
            for n in range(first, last + 1)
        ]
    )


def shown_diff(
    *files: tuple[str, Sequence[tuple[int, int]]], status: str = "modified"
) -> PromptDiff:
    """What the model was shown: ``("a.py", [(1, 40), (60, 80)])`` is a file with two hunks."""
    return PromptDiff(
        files=[
            ShownFile(path=path, status=status, hunks=[hunk(a, b) for a, b in ranges])
            for path, ranges in files
        ],
        token_count=100,
    )


def pull_diff(*files: DiffFile, truncated: bool = False) -> PullDiff:
    return PullDiff(files=list(files), truncated=truncated)


def patch_of(*hunks: tuple[int, int, list[str]]) -> str:
    """A ``patch`` like GitHub's: no trailing newline. Each hunk is ``(old_start, new_start,
    lines)`` with the lines already prefixed by `` ``, ``+`` or ``-``; the counts are computed."""
    out: list[str] = []
    for old_start, new_start, lines in hunks:
        old = sum(1 for line in lines if line[:1] in (" ", "-"))
        new = sum(1 for line in lines if line[:1] in (" ", "+"))
        out.append(f"@@ -{old_start},{old} +{new_start},{new} @@")
        out.extend(lines)
    return "\n".join(out)
