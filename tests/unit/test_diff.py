"""The patch parser and the selection of the hunks shown to the model (D-97)."""

import json
from pathlib import Path

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.diff import (
    parse_patch,
    render_diff_line,
    select_for_prompt,
    usable_path,
)
from cicd_doctor.core.models import DiffFile, DiffLine, LogExcerpt
from tests.diffs import patch_of, pull_diff
from tests.llmcases import make_excerpt

FIXTURES = Path(__file__).parents[1] / "fixtures" / "github"


def _numbers(patch: str) -> list[list[tuple[int, str, bool]]]:
    return [[(line.number, line.text, line.added) for line in h.lines] for h in parse_patch(patch)]


# --- the parser -----------------------------------------------------------------------------


def test_one_hunk_gives_the_numbers_of_its_new_side() -> None:
    patch = patch_of((10, 10, [" keep", "-gone", "+new one", "+new two", " tail"]))

    assert _numbers(patch) == [
        [(10, "keep", False), (11, "new one", True), (12, "new two", True), (13, "tail", False)]
    ]


def test_several_hunks_each_start_at_their_own_header() -> None:
    patch = patch_of((1, 1, [" a", "+b"]), (40, 41, ["-x", "+y", " z"]))

    hunks = parse_patch(patch)

    assert [(h.first, h.last) for h in hunks] == [(1, 2), (41, 42)]


def test_a_removed_line_takes_no_number_and_a_blank_context_line_is_a_line() -> None:
    patch = "@@ -5,3 +5,2 @@\n a\n-b\n "  # the blank context line is a single space

    assert _numbers(patch) == [[(5, "a", False), (6, "", False)]]


def test_a_header_without_counts_means_one_line() -> None:
    assert _numbers("@@ -7 +7 @@\n-old\n+new") == [[(7, "new", True)]]


def test_the_no_newline_marker_is_not_a_line() -> None:
    patch = (
        "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file"
    )

    assert _numbers(patch) == [[(1, "a", False), (2, "c", True)]]


def test_a_trailing_newline_does_not_add_a_line() -> None:
    assert _numbers("@@ -0,0 +1 @@\n+x\n") == [[(1, "x", True)]]


def test_a_new_file_starts_at_line_one() -> None:
    patch = patch_of((0, 1, ["+a", "+b", "+c"]))

    assert [(h.first, h.last) for h in parse_patch(patch)] == [(1, 3)]


def test_a_pure_deletion_has_no_new_side_and_gives_no_hunk() -> None:
    assert parse_patch("@@ -3,2 +2,0 @@\n-a\n-b") == []


@pytest.mark.parametrize("patch", [None, ""])
def test_a_file_without_patch_has_no_hunks(patch: str | None) -> None:
    assert parse_patch(patch) == []


@pytest.mark.parametrize(
    "patch",
    [
        "not a patch",
        "@@ -1,2 +1,2 @@\n a",  # the header promises two lines
        "@@ -1 +1 @@\n+a\n+b",  # a line more than the header says
        "@@ -1 +1 @@\n?weird prefix",
        "@@ -1 +1 @@\n+a\nstray text between hunks\n@@ -9 +9 @@\n+b",
        "@@ -1 +1 @@\n+a\n@@ garbage @@\n+b",
    ],
)
def test_a_patch_that_does_not_follow_its_headers_gives_no_hunks_at_all(patch: str) -> None:
    assert parse_patch(patch) == []


def test_a_line_that_looks_like_a_header_inside_a_hunk_is_just_a_line() -> None:
    patch = "@@ -1 +1,2 @@\n a\n+@@ -9 +9 @@ fake"

    assert _numbers(patch) == [[(1, "a", False), (2, "@@ -9 +9 @@ fake", True)]]


@pytest.mark.parametrize("name", ["pulls_files_pr34", "pulls_files_pr36"])
def test_every_real_patch_matches_its_headers(name: str) -> None:
    files = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert files

    for item in files:
        hunks = parse_patch(item["patch"])
        headers = [line for line in item["patch"].split("\n") if line.startswith("@@ ")]
        assert len(hunks) == len(headers)
        for hunk, header in zip(hunks, headers, strict=True):
            new = header.split("+")[1].split(" ")[0]
            start, _, count = new.partition(",")
            assert hunk.first == int(start)
            assert len(hunk.lines) == int(count or 1)
            assert [line.number for line in hunk.lines] == list(range(hunk.first, hunk.last + 1))


# --- what the model is shown ----------------------------------------------------------------


def _file(name: str, *hunks: tuple[int, int, list[str]], status: str = "modified") -> DiffFile:
    return DiffFile(filename=name, status=status, patch=patch_of(*hunks))


def _log(*lines: str) -> LogExcerpt:
    return make_excerpt(list(lines))


def test_only_the_files_the_log_names_are_shown() -> None:
    diff = pull_diff(
        _file("src/a.py", (1, 1, [" x", "+y"])),
        _file("src/b.py", (1, 1, [" x", "+y"])),
        _file("docs/other.md", (1, 1, [" x", "+y"])),
    )
    excerpt = _log('  File "src/b.py", line 2, in run', "ValueError: bad")

    shown = select_for_prompt(diff, excerpt, CharTokenCounter())

    assert shown is not None
    assert [f.path for f in shown.files] == ["src/b.py"]


def test_a_name_must_match_as_a_whole_path() -> None:
    diff = pull_diff(_file("a.py", (1, 1, [" x", "+y"])))

    assert select_for_prompt(diff, _log("error in data.py:3"), CharTokenCounter()) is None
    assert select_for_prompt(diff, _log("error in xa.py:3"), CharTokenCounter()) is None
    assert select_for_prompt(diff, _log("error in ./a.py:3"), CharTokenCounter()) is not None
    assert (
        select_for_prompt(
            diff, _log("FAILED /home/runner/work/r/r/a.py::test_x"), CharTokenCounter()
        )
        is not None
    )


def test_a_truncated_diff_a_missing_diff_and_no_named_file_give_nothing() -> None:
    diff = pull_diff(_file("a.py", (1, 1, [" x", "+y"])), truncated=True)
    counter = CharTokenCounter()

    assert select_for_prompt(diff, _log("a.py:1 failed"), counter) is None
    assert select_for_prompt(None, _log("a.py:1 failed"), counter) is None
    assert select_for_prompt(pull_diff(), _log("a.py:1 failed"), counter) is None
    assert (
        select_for_prompt(pull_diff(_file("a.py", (1, 1, ["+y"]))), _log("nothing"), counter)
        is None
    )


def test_removed_files_files_without_patch_and_odd_paths_are_never_shown() -> None:
    diff = pull_diff(
        _file("gone.py", (1, 1, [" x", "+y"]), status="removed"),
        DiffFile(filename="uv.lock", status="modified", patch=None),
        DiffFile(filename="moved.py", previous_filename="old.py", status="renamed"),
        _file("bad\nname.py", (1, 1, [" x", "+y"])),
        _file("ok.py", (1, 1, [" x", "+y"])),
    )
    log = _log("gone.py uv.lock moved.py old.py bad", "name.py ok.py")

    shown = select_for_prompt(diff, log, CharTokenCounter())

    assert shown is not None
    assert [f.path for f in shown.files] == ["ok.py"]


def test_a_renamed_file_with_changes_is_shown_by_its_new_name_only() -> None:
    diff = pull_diff(
        DiffFile(
            filename="new/name.py",
            previous_filename="old/name.py",
            status="renamed",
            patch=patch_of((1, 1, [" x", "+y"])),
        )
    )

    by_old = select_for_prompt(diff, _log("old/name.py:1 failed"), CharTokenCounter())
    by_new = select_for_prompt(diff, _log("new/name.py:1 failed"), CharTokenCounter())

    assert by_old is None
    assert by_new is not None
    assert by_new.files[0].path == "new/name.py"


def test_the_hunk_the_log_points_at_comes_first_and_the_budget_drops_whole_hunks() -> None:
    big = [f"+line {i} " + "x" * 30 for i in range(30)]
    diff = pull_diff(_file("a.py", (1, 1, big), (100, 130, big), (300, 330, [" tail", "+end"])))
    excerpt = _log("a.py:305: boom")

    shown = select_for_prompt(diff, excerpt, CharTokenCounter(), max_lines=60, max_tokens=10_000)

    assert shown is not None
    hunks = shown.files[0].hunks
    # the pointed hunk (300-301) and the first one that fits; the third does not fit in 60 lines
    assert [(h.first, h.last) for h in hunks] == [(1, 30), (330, 331)]
    assert all(len(h.lines) == h.last - h.first + 1 for h in hunks)  # whole hunks only


def test_the_token_bound_is_applied_to_the_rendered_block() -> None:
    lines = ["+" + "word " * 60 for _ in range(10)]  # long, like documentation lines
    diff = pull_diff(_file("a.py", (1, 1, lines)))

    unbounded = select_for_prompt(diff, _log("a.py:1"), CharTokenCounter(), max_tokens=100_000)
    bounded = select_for_prompt(diff, _log("a.py:1"), CharTokenCounter(), max_tokens=100)

    assert unbounded is not None
    assert unbounded.token_count > 100
    assert bounded is None  # the only hunk does not fit: nothing partial is shown


def test_the_number_of_files_is_bounded_and_the_token_count_is_reported() -> None:
    diff = pull_diff(*[_file(f"f{i}.py", (1, 1, [" x", "+y"])) for i in range(8)])
    excerpt = _log(*[f"f{i}.py:1" for i in range(8)])

    shown = select_for_prompt(diff, excerpt, CharTokenCounter(), max_files=3)

    assert shown is not None
    assert [f.path for f in shown.files] == ["f0.py", "f1.py", "f2.py"]
    assert shown.token_count > 0


def test_a_diff_of_three_thousand_files_is_selected_without_trouble() -> None:
    diff = pull_diff(*[_file(f"d/f{i}.py", (1, 1, [" x", "+y"])) for i in range(3000)])

    shown = select_for_prompt(diff, _log("d/f2999.py:1 failed"), CharTokenCounter())

    assert shown is not None
    assert [f.path for f in shown.files] == ["d/f2999.py"]


def test_a_line_is_rendered_with_its_number_and_a_marker() -> None:
    assert render_diff_line(DiffLine(number=12, text="keep", added=False)) == "12: keep"
    assert render_diff_line(DiffLine(number=13, text="new", added=True)) == "13+ new"


def test_controls_and_long_lines_are_cleaned_when_rendered() -> None:
    line = DiffLine(number=1, text="a\x1b[31m\tb\x00" + "z" * 500, added=True)

    rendered = render_diff_line(line)

    assert "\x1b" not in rendered
    assert "\x00" not in rendered
    assert "\t" not in rendered
    assert len(rendered) <= 210


@pytest.mark.parametrize(
    ("path", "usable"),
    [
        ("src/a.py", True),
        ("a b/c.txt", True),
        ("", False),
        ("a\nb", False),
        ("a" + chr(0x2028) + "file: .github/ci.yml (added)", False),  # a line break for a model
        ("a" + chr(0x85) + "b", False),
        ("a" + chr(0x202E) + "b", False),
        ("a" + chr(0x200B) + "b", False),
        ("x" * 300, False),
    ],
)
def test_a_usable_path_is_short_and_has_no_controls(path: str, usable: bool) -> None:
    assert usable_path(path) is usable
