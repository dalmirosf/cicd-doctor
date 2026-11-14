"""Redaction (SPEC §9.2, D-12, D-59): the corpus, the property tests and the PEM handling."""

import math
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cicd_doctor.core.redaction import (
    KINDS,
    entropy_threshold,
    placeholder,
    redact,
    redact_line,
    redact_lines,
    redact_text,
    shannon_entropy,
)
from tests.redaction_corpus import (
    GENERIC_VALUES,
    LOOKALIKES,
    SECRET_KEYS,
    SECRET_SHAPES,
    SECRET_SNIPPETS,
    SEPARATORS,
    SPACERS,
    WORDS,
    Planted,
    generic_threshold_cases,
    pem_edge_cases,
    planted_pem_block,
    planted_secrets,
)

PLANTED = planted_secrets()


# --- corpus: 100% of planted secrets redacted, 0 look-alikes touched -------------------------


def test_corpus_covers_every_secret_kind_at_least_twice() -> None:
    kinds = [planted.kind for planted in PLANTED]
    assert {kind for kind in KINDS if kinds.count(kind) < 2} == set()


def test_corpus_every_planted_secret_is_redacted() -> None:
    leaked = [p.kind for p in PLANTED if p.secret in redact(p.line)]
    redacted_share = 1 - len(leaked) / len(PLANTED)
    assert redacted_share == 1.0, f"leaked: {leaked}"


@pytest.mark.parametrize("planted", PLANTED, ids=lambda p: f"{p.kind}")
def test_corpus_secret_is_replaced_by_its_kind(planted: Planted) -> None:
    result = redact_line(planted.line)

    assert planted.secret not in result.text
    assert placeholder(planted.kind) in result.text
    assert result.count >= 1


def test_corpus_no_lookalike_is_redacted() -> None:
    touched = [line for line in LOOKALIKES if redact(line) != line]
    assert touched == []


def test_corpus_multi_line_pem_block_is_redacted() -> None:
    lines, body = planted_pem_block()

    result = redact_text("\n".join(lines))

    assert not any(secret in result.text for secret in body)
    out = result.text.split("\n")
    assert out[0] == lines[0]
    assert out[-1] == "Done"
    assert len(out) == len(lines)  # line numbers are preserved
    assert result.count == len(lines) - 2  # each line of the block counts once


# --- property tests ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    kind=st.sampled_from(sorted(SECRET_SHAPES)),
    data=st.data(),
    before=WORDS,
    after=WORDS,
    left=SPACERS,
    right=SPACERS,
)
def test_pattern_shaped_secrets_never_leak(
    kind: str, data: st.DataObject, before: str, after: str, left: str, right: str
) -> None:
    secret = data.draw(SECRET_SHAPES[kind], label="secret")
    line = f"{before}{left}{secret}{right}{after}"

    result = redact_line(line)

    assert secret not in result.text
    assert result.count >= 1


@settings(max_examples=60, deadline=None)
@given(
    key=st.sampled_from(SECRET_KEYS), separator=st.sampled_from(SEPARATORS), value=GENERIC_VALUES
)
def test_values_of_secret_named_keys_never_leak(key: str, separator: str, value: str) -> None:
    line = f"export {key}{separator}{value}"

    assert value not in redact(line)


@settings(max_examples=40, deadline=None)
@given(
    user=st.text(alphabet="abcdefghij0123456789", min_size=1, max_size=12),
    password=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!*", min_size=8, max_size=30),
    scheme=st.sampled_from(["https", "postgresql", "redis", "amqp", "git+https"]),
)
def test_url_credentials_never_leak(user: str, password: str, scheme: str) -> None:
    line = f"connecting to {scheme}://{user}:{password}@host.example.com:5432/db"

    result = redact(line)

    assert password not in result
    assert f"{scheme}://{placeholder('url_credentials')}@host.example.com" in result


@settings(max_examples=60, deadline=None)
@given(sha=st.text(alphabet="0123456789abcdef", min_size=40, max_size=40), words=WORDS)
def test_git_shas_are_never_redacted(sha: str, words: str) -> None:
    for line in (f"HEAD is now at {sha} {words}", f"commit {sha}", f"GITHUB_SHA={sha}"):
        assert redact(line) == line


@settings(max_examples=60, deadline=None)
@given(value=st.uuids(), words=WORDS)
def test_uuids_are_never_redacted_by_shape(value: uuid.UUID, words: str) -> None:
    for line in (f"request-id: {value}", f"{words} {value}", f"uuid={value}"):
        assert redact(line) == line


log_like = st.lists(
    st.one_of(
        WORDS,
        st.sampled_from(sorted(SECRET_SHAPES)).flatmap(lambda kind: SECRET_SHAPES[kind]),
        st.builds(
            lambda k, s, v: f"{k}{s}{v}",
            st.sampled_from(SECRET_KEYS),
            st.sampled_from(SEPARATORS),
            GENERIC_VALUES,
        ),
        st.sampled_from(LOOKALIKES),
        st.sampled_from(SECRET_SNIPPETS),
    ),
    max_size=6,
).map(" ".join)


@settings(max_examples=100, deadline=None)
@given(lines=st.lists(st.one_of(log_like, st.text(max_size=40)), max_size=8))
def test_redaction_is_idempotent(lines: list[str]) -> None:
    text = "\n".join(line.replace("\n", " ").replace("\r", " ") for line in lines)

    once = redact_text(text)
    twice = redact_text(once.text)

    assert twice.text == once.text
    assert twice.count == 0


# --- details ----------------------------------------------------------------------------------


def test_github_masks_are_kept() -> None:
    assert redact("password: ***") == "password: ***"
    assert redact("token=***abc123***def") == "token=***abc123***def"


@pytest.mark.parametrize(
    "line",
    [
        "AUTHORIZATION: basic ***",
        "authorization: Bearer ***",
        "aws_secret_access_key = ***",
        "https://x-access-token:***@github.com/acme/widgets.git",
    ],
)
def test_github_masks_are_not_reported_as_redactions(line: str) -> None:
    result = redact_line(line)

    assert result.text == line
    assert result.count == 0  # the checkout step of every job has one of these lines


def test_a_real_secret_next_to_a_mask_is_still_redacted() -> None:
    planted = next(p for p in PLANTED if p.kind == "authorization")
    line = f"AUTHORIZATION: basic *** then Authorization: Bearer {planted.secret}"

    result = redact_line(line)

    assert planted.secret not in result.text
    assert "basic ***" in result.text
    assert result.count == 1


@pytest.mark.parametrize(
    "line",
    [
        "PWD=/home/runner/work/widgets/widgets",
        r"secret: C:\Users\runneradmin\secrets\file.txt",
        "token=${GITHUB_TOKEN}",
        "password=%DB_PASSWORD%",
        "api_key: ~/keys/dev.key",
        "token: short",  # under 8 characters
        "password: aaaaaaaaaaaa",  # far too little entropy for its length
    ],
)
def test_generic_assignments_skip_references_paths_and_low_entropy(line: str) -> None:
    assert redact(line) == line


@pytest.mark.parametrize(("value", "redacted"), generic_threshold_cases())
def test_generic_threshold_depends_on_the_length(value: str, redacted: bool) -> None:
    assert (value not in redact(f"password={value}")) is redacted


def test_entropy_threshold_is_capped_at_three_and_a_half_bits() -> None:
    assert entropy_threshold(8) == pytest.approx(2.0)
    assert entropy_threshold(16) == pytest.approx(3.0)
    assert entropy_threshold(1000) == pytest.approx(3.5)
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("abcd") == pytest.approx(math.log2(4))


def test_pem_block_without_end_stops_at_the_first_non_pem_line() -> None:
    lines, _ = pem_edge_cases()

    out = [item.text for _, item in redact_lines(lines)]

    assert out == [placeholder("private_key"), placeholder("private_key"), "Done."]


def test_pem_begin_and_end_lines_keep_their_surroundings() -> None:
    _, lines = pem_edge_cases()

    out = [item.text for _, item in redact_lines(lines)]

    marker = placeholder("private_key")
    assert out == [f"key: {marker}", marker, f"{marker} (written)"]


def test_redact_lines_redacts_a_selected_line_inside_a_pem_block_it_cannot_see_start() -> None:
    lines, _body = planted_pem_block()

    selected = redact_lines(lines, only=[3, 4, 999, -1])

    assert [index for index, _ in selected] == [3, 4]
    assert all(item.text == placeholder("private_key") for _, item in selected)


def test_unrelated_lines_are_returned_unchanged_and_uncounted() -> None:
    result = redact_line("collected 12 items")

    assert result.text == "collected 12 items"
    assert result.count == 0
