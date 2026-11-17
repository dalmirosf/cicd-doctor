"""``reuse_cached``: a cached LLM diagnosis made safe for the excerpt of a new run (D-81)."""

from hypothesis import given
from hypothesis import strategies as st

from cicd_doctor.core.diagnosis.cache import reuse_cached
from cicd_doctor.core.models import Evidence, LogExcerpt, LogLine, Suggestion
from tests.factories import make_diagnosis, make_failure

ERROR = "ModuleNotFoundError: No module named 'requests'"


def excerpt(*lines: tuple[int, str]) -> LogExcerpt:
    base = make_failure().excerpt
    return base.model_copy(
        update={"lines": [LogLine(number=n, text=t) for n, t in lines], "error_lines": []}
    )


def cached(*evidence: tuple[int, str], **changes: object):  # type: ignore[no-untyped-def]
    return make_diagnosis(
        evidence=[Evidence(line=n, text=t) for n, t in evidence], evidence_verified=True, **changes
    )


def test_the_evidence_is_anchored_to_the_line_of_the_new_excerpt() -> None:
    old = cached((10, ERROR), model="gpt-x", prompt_version="diag-v1", confidence=0.77)

    result = reuse_cached(old, excerpt((3, "Run pytest"), (42, ERROR)))

    assert result is not None
    assert [(e.line, e.text) for e in result.evidence] == [(42, ERROR)]
    assert result.source == "cache"
    assert result.evidence_verified is True
    assert (result.model, result.prompt_version, result.confidence) == ("gpt-x", "diag-v1", 0.77)
    assert result.category == old.category
    assert result.probable_cause == old.probable_cause
    assert result.suggested_fix == old.suggested_fix


def test_the_text_is_found_inside_a_longer_line_with_other_whitespace() -> None:
    old = cached((10, "No module named   'requests'"))

    result = reuse_cached(old, excerpt((7, f"  {ERROR}   ")))

    assert result is not None
    assert [e.line for e in result.evidence] == [7]


def test_evidence_that_is_no_longer_in_the_excerpt_is_dropped() -> None:
    old = cached((10, ERROR), (12, "Segmentation fault (core dumped)"))

    result = reuse_cached(old, excerpt((5, ERROR)))

    assert result is not None
    assert [(e.line, e.text) for e in result.evidence] == [(5, ERROR)]


def test_without_any_evidence_left_the_cache_does_not_apply() -> None:
    assert reuse_cached(cached((10, ERROR)), excerpt((1, "something else entirely"))) is None
    assert reuse_cached(cached(), excerpt((1, ERROR))) is None
    assert reuse_cached(cached((10, "   ")), excerpt((1, ERROR))) is None


def test_the_suggestion_is_removed() -> None:
    old = cached(
        (10, ERROR),
        suggestion=Suggestion(path="requirements.txt", start_line=1, end_line=2, replacement="x"),
    )

    result = reuse_cached(old, excerpt((10, ERROR)))

    assert result is not None
    assert result.suggestion is None
    assert result.suggested_fix == old.suggested_fix


def test_the_closest_line_wins_when_the_text_repeats_and_it_is_used_once() -> None:
    old = cached((50, ERROR), (51, ERROR))

    result = reuse_cached(old, excerpt((1, ERROR), (49, ERROR), (90, ERROR)))

    assert result is not None
    assert [e.line for e in result.evidence] == [49, 90]


def test_a_copied_line_prefix_is_ignored_like_in_the_validation_of_a_model_answer() -> None:
    old = cached((10, f"L10: {ERROR}"))

    result = reuse_cached(old, excerpt((4, ERROR)))

    assert result is not None
    assert [e.line for e in result.evidence] == [4]


_TEXTS = st.text(alphabet="abc de'_:", min_size=1, max_size=12)


@given(
    old=st.lists(st.tuples(st.integers(1, 30), _TEXTS), max_size=8),
    new=st.lists(st.tuples(st.integers(1, 30), _TEXTS), max_size=12, unique_by=lambda x: x[0]),
)
def test_every_evidence_of_the_result_is_a_quote_of_its_line(
    old: list[tuple[int, str]], new: list[tuple[int, str]]
) -> None:
    new_excerpt = excerpt(*new)
    result = reuse_cached(cached(*old), new_excerpt)

    if result is None:
        return
    lines = {line.number: " ".join(line.text.split()) for line in new_excerpt.lines}
    assert result.evidence
    assert result.suggestion is None
    for item in result.evidence:
        assert item.line in lines
        assert " ".join(item.text.split()) in lines[item.line]
