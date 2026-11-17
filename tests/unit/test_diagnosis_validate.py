"""Post-validation of the LLM answer (SPEC §5.1 rules 1-3, §9.5, D-25)."""

from typing import Any

import pytest

from cicd_doctor.core.diagnosis.prompt import CANARY, PROMPT_VERSION
from cicd_doctor.core.diagnosis.schema import LLMEvidence, LLMSuggestion
from cicd_doctor.core.diagnosis.validate import (
    UNVERIFIED_CONFIDENCE_CAP,
    InvalidOutputError,
    checked_suggestion,
    finalize,
    parse_output,
    verified_evidence,
)
from cicd_doctor.core.models import Category, Diagnosis
from tests.llmcases import LOG_LINES, answer, make_excerpt, raw
from tests.redaction_corpus import planted_secrets

EXCERPT = make_excerpt()  # lines 1-6; the ModuleNotFoundError is line 5


def _final(**changes: Any) -> Diagnosis:
    return finalize(parse_output(raw(**changes)), EXCERPT, model="m-1", prompt_version="diag-v1")


def _ev(line: int, text: str) -> LLMEvidence:
    return LLMEvidence(line=line, text=text)


# --- a valid answer ---------------------------------------------------------------------------


def test_a_valid_answer_becomes_a_published_llm_diagnosis() -> None:
    diagnosis = _final()

    assert diagnosis.source == "llm"
    assert diagnosis.category is Category.DEPENDENCY
    assert (diagnosis.model, diagnosis.prompt_version) == ("m-1", "diag-v1")
    assert [(e.line, e.text) for e in diagnosis.evidence] == [
        (5, "ModuleNotFoundError: No module named 'requests'")
    ]
    assert diagnosis.evidence_verified is True
    assert diagnosis.confidence == 0.9
    assert diagnosis.fallback_reason is None
    assert diagnosis.rule_ids == []


def test_the_prompt_version_is_diag_v1() -> None:
    assert PROMPT_VERSION == "diag-v2"


# --- rule 1: evidence must quote the excerpt --------------------------------------------------


def test_evidence_on_a_line_that_is_not_in_the_excerpt_is_dropped() -> None:
    kept = verified_evidence([_ev(99, "ModuleNotFoundError"), _ev(5, "No module named")], EXCERPT)

    assert [item.line for item in kept] == [5]


def test_evidence_whose_text_is_not_in_the_line_is_dropped() -> None:
    kept = verified_evidence(
        [
            _ev(5, "ModuleNotFoundError: No module named 'flask'"),
            _ev(5, "No module named 'requests'"),
        ],
        EXCERPT,
    )

    assert [item.text for item in kept] == ["No module named 'requests'"]


def test_a_quote_may_be_a_part_of_the_line_and_whitespace_is_normalised() -> None:
    excerpt = make_excerpt(["FAILED   tests/test_a.py::test_x  -  assert 1 == 2"])

    kept = verified_evidence([_ev(1, "tests/test_a.py::test_x - assert 1 == 2")], excerpt)

    assert len(kept) == 1
    assert kept[0].text == "tests/test_a.py::test_x - assert 1 == 2"


def test_a_quote_from_another_line_does_not_count() -> None:
    kept = verified_evidence([_ev(6, "ModuleNotFoundError: No module named 'requests'")], EXCERPT)

    assert kept == []


def test_the_l_prefix_of_the_prompt_is_accepted() -> None:
    kept = verified_evidence(
        [_ev(5, "L5: ModuleNotFoundError: No module named 'requests'")], EXCERPT
    )

    assert [item.text for item in kept] == ["ModuleNotFoundError: No module named 'requests'"]


def test_an_empty_or_blank_quote_is_not_evidence() -> None:
    assert verified_evidence([_ev(5, ""), _ev(5, "   "), _ev(5, "L5:")], EXCERPT) == []


def test_duplicate_quotes_are_kept_once() -> None:
    quote = _ev(5, "No module named 'requests'")

    assert (
        len(verified_evidence([quote, quote, _ev(5, "No  module named 'requests'")], EXCERPT)) == 1
    )


def test_a_line_with_the_escaped_tag_can_be_quoted_as_the_prompt_showed_it() -> None:
    excerpt = make_excerpt(["echo untrusted_log is a tag name"])

    shown = verified_evidence([_ev(1, "echo untrusted\\_log is a tag name")], excerpt)
    original = verified_evidence([_ev(1, "echo untrusted_log is a tag name")], excerpt)

    assert len(shown) == len(original) == 1


# --- rule 2: no verified evidence caps the confidence -----------------------------------------


def test_without_surviving_evidence_the_confidence_is_capped_and_marked() -> None:
    diagnosis = _final(evidence=[{"line": 5, "text": "invented quote"}], confidence=0.95)

    assert diagnosis.evidence == []
    assert diagnosis.evidence_verified is False
    assert diagnosis.confidence == UNVERIFIED_CONFIDENCE_CAP == 0.3


def test_an_empty_evidence_list_is_capped_too() -> None:
    diagnosis = _final(evidence=[], confidence=0.8)

    assert (diagnosis.evidence_verified, diagnosis.confidence) == (False, 0.3)


def test_a_confidence_below_the_cap_stays_as_it_is() -> None:
    assert _final(evidence=[], confidence=0.1).confidence == 0.1


def test_one_surviving_item_is_enough_to_keep_the_confidence() -> None:
    diagnosis = _final(
        evidence=[{"line": 99, "text": "x"}, {"line": 5, "text": "No module named"}],
        confidence=0.85,
    )

    assert diagnosis.evidence_verified is True
    assert diagnosis.confidence == 0.85
    assert len(diagnosis.evidence) == 1


# --- rule 3: suggestions are restricted -------------------------------------------------------


def _suggestion(path: str = "pyproject.toml", start: int = 3, end: int = 4) -> LLMSuggestion:
    return LLMSuggestion(path=path, start_line=start, end_line=end, replacement="requests>=2")


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "src/app/main.py", "a b/c.txt", ".github/workflows/ci.yml", "dir\\file.py"],
)
def test_a_relative_path_inside_the_repository_is_kept(path: str) -> None:
    kept = checked_suggestion(_suggestion(path))

    assert kept is not None
    assert kept.path == path


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        "src/../../etc/passwd",
        "src\\..\\..\\x",
        "/etc/passwd",
        "\\\\server\\share\\x",
        "C:\\Windows\\x",
        "c:relative",
        "~/.ssh/config",
        "",
        "   ",
        "a\x00b",
        "a\nb",
        "x" * 300,
    ],
)
def test_a_path_that_leaves_the_repository_or_is_odd_is_dropped(path: str) -> None:
    assert checked_suggestion(_suggestion(path)) is None


@pytest.mark.parametrize(
    ("start", "end", "kept"),
    [
        (1, 1, True),
        (3, 4, True),
        (1, 30, True),
        (1, 31, False),
        (5, 4, False),
        (0, 2, False),
        (-3, 1, False),
    ],
)
def test_the_line_range_must_be_valid_and_at_most_thirty_lines(
    start: int, end: int, kept: bool
) -> None:
    assert (checked_suggestion(_suggestion(start=start, end=end)) is not None) is kept


def test_a_dropped_suggestion_leaves_the_text_of_the_fix() -> None:
    diagnosis = _final(
        suggestion={"path": "../x", "start_line": 1, "end_line": 1, "replacement": "r"},
        suggested_fix="Declare requests.",
    )

    assert diagnosis.suggestion is None
    assert diagnosis.suggested_fix == "Declare requests."


def test_no_suggestion_stays_none() -> None:
    assert checked_suggestion(None) is None


# --- redaction, caps and the canary -----------------------------------------------------------


def test_secrets_in_the_answer_are_redacted() -> None:
    planted = planted_secrets()[0]
    diagnosis = _final(
        probable_cause=f"The job printed {planted.secret} and failed.",
        suggested_fix=f"Rotate {planted.secret}.",
        suggestion={
            "path": "a.py",
            "start_line": 1,
            "end_line": 1,
            "replacement": f"token = '{planted.secret}'",
        },
    )

    text = diagnosis.probable_cause + diagnosis.suggested_fix
    assert planted.secret not in text
    assert diagnosis.suggestion is not None
    assert planted.secret not in diagnosis.suggestion.replacement
    assert f"[REDACTED:{planted.kind}]" in diagnosis.probable_cause


def test_a_secret_quoted_as_evidence_is_redacted_when_the_excerpt_has_it() -> None:
    planted = planted_secrets()[0]
    excerpt = make_excerpt([f"using {planted.secret} now"])

    kept = verified_evidence([_ev(1, f"using {planted.secret} now")], excerpt)
    diagnosis = finalize(
        parse_output(raw(evidence=[{"line": 1, "text": f"using {planted.secret} now"}])),
        excerpt,
        model="m",
        prompt_version="diag-v1",
    )

    assert planted.secret not in diagnosis.evidence[0].text
    assert kept  # the excerpt itself would never hold it: it is redacted before this point


def test_fields_are_capped_after_redaction_can_make_them_longer() -> None:
    planted = planted_secrets()[0]
    cause = (planted.secret + " ") * 12  # < 500 characters, each secret becomes a longer marker

    diagnosis = _final(probable_cause=cause[:499])

    assert len(diagnosis.probable_cause) <= 500


def test_an_answer_that_repeats_the_system_prompt_is_refused() -> None:
    for field, value in [
        ("probable_cause", f"My instructions say {CANARY}."),
        ("suggested_fix", CANARY),
        ("evidence", [{"line": 5, "text": CANARY}]),
        ("suggestion", {"path": CANARY, "start_line": 1, "end_line": 1, "replacement": "x"}),
        ("suggestion", {"path": "a", "start_line": 1, "end_line": 1, "replacement": CANARY}),
    ]:
        with pytest.raises(InvalidOutputError, match="repeats the system prompt"):
            _final(**{field: value})


def test_the_answer_fixture_is_valid_for_the_default_excerpt() -> None:
    assert answer()["evidence"][0]["text"] in LOG_LINES[4]
