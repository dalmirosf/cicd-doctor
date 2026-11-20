"""The repository configuration file: parsing, validation and the cache (D-90)."""

from datetime import timedelta

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory.repo_config import FakeRepoConfig
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import LoadedConfig, RepoConfig, RepoRef
from cicd_doctor.core.repo_config import (
    MAX_CONFIG_BYTES,
    CachedRepoConfig,
    effective_llm,
    ignores,
    parse_repo_config,
)


def valid_file(**fields: object) -> LoadedConfig:
    return LoadedConfig(config=RepoConfig(**fields), state="file")  # type: ignore[arg-type]


# --- valid and empty files -----------------------------------------------------------------------


def test_a_full_file_is_read() -> None:
    text = "comment: false\nllm: on_demand\nignore_workflows:\n  - Nightly\n  - 'Release (prod)'\n"
    loaded = parse_repo_config(text)
    assert loaded == valid_file(
        comment=False, llm="on_demand", ignore_workflows=["Nightly", "Release (prod)"]
    )
    assert loaded.problems == ()


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "---\n", "﻿"])
def test_an_empty_file_is_the_same_as_no_file(text: str) -> None:
    assert parse_repo_config(text) == LoadedConfig()


def test_a_partial_file_keeps_the_other_defaults() -> None:
    assert parse_repo_config("llm: off\n") == valid_file(llm="off")


def test_a_bom_and_crlf_line_endings_are_accepted() -> None:
    assert parse_repo_config("﻿comment: false\r\nllm: auto\r\n") == valid_file(comment=False)


def test_odd_characters_in_a_workflow_name_are_kept_verbatim() -> None:
    loaded = parse_repo_config('ignore_workflows: ["Build & test: <all>", "日本語 ✅", "a`b"]\n')
    assert loaded.config.ignore_workflows == ["Build & test: <all>", "日本語 ✅", "a`b"]


# --- invalid files turn into ``invalid`` with field names only -----------------------------------


@pytest.mark.parametrize(
    ("text", "problems"),
    [
        ("unknown_key: 1\n", ("unknown_key",)),
        ("comment: maybe\n", ("comment",)),
        ("comment: 1\n", ("comment",)),  # strict: no coercion
        ("llm: sometimes\n", ("llm",)),
        ("llm: 5\n", ("llm",)),
        ("ignore_workflows: Nightly\n", ("ignore_workflows",)),
        ("ignore_workflows: [1, Nightly]\n", ("ignore_workflows[0]",)),
        ("ignore_workflows: ['']\n", ("ignore_workflows",)),
        ("- a\n- b\n", ("root",)),
        ("just text\n", ("root",)),
        ("comment: [\n", ("yaml",)),
        ("comment: true\n\tllm: off\n", ("yaml",)),
        ("comment: true\n---\nllm: off\n", ("documents",)),
        ("defaults: &d {comment: false}\nllm: auto\n", ("anchors",)),
        ("comment: *d\n", ("aliases",)),
        ("comment: !!python/object:os.system x\n", ("tags",)),
        ("comment: !!bool true\n", ("tags",)),
        ("llm: off\nllm: auto\n", ("duplicate:llm",)),
        ("? [a]\n: 1\n", ("key",)),
        ("<<: {llm: off}\n", ("merge",)),
        ('ignore_workflows: ["a\\u0007b"]\n', ("ignore_workflows",)),
        ("ignore_workflows: [a\u0007]\n", ("yaml",)),  # a control character is not YAML
    ],
)
def test_an_invalid_file_names_the_failing_fields(text: str, problems: tuple[str, ...]) -> None:
    loaded = parse_repo_config(text)
    assert loaded.state == "invalid"
    assert loaded.problems == problems
    assert loaded.config == RepoConfig()  # the defaults; the caller turns the LLM off


def test_too_many_names_and_names_too_long_are_invalid() -> None:
    many = "ignore_workflows: [" + ", ".join(f"w{i}" for i in range(51)) + "]\n"
    assert parse_repo_config(many).problems == ("ignore_workflows",)
    fifty = "ignore_workflows: [" + ", ".join(f"w{i}" for i in range(50)) + "]\n"
    assert parse_repo_config(fifty).state == "file"
    assert parse_repo_config(f"ignore_workflows: ['{'x' * 121}']\n").state == "invalid"
    assert parse_repo_config(f"ignore_workflows: ['{'x' * 120}']\n").state == "file"


def test_a_file_over_the_cap_is_invalid_before_it_is_parsed() -> None:
    text = "# " + "x" * MAX_CONFIG_BYTES + "\nllm: off\n"
    assert parse_repo_config(text).problems == ("size",)


def test_a_flat_document_with_too_many_nodes_is_invalid() -> None:
    text = "ignore_workflows: [" + ",".join(["a"] * 3000) + "]\n"
    assert len(text) < MAX_CONFIG_BYTES
    assert parse_repo_config(text).problems == ("size",)


def test_deep_nesting_is_invalid_and_never_raises() -> None:
    text = "a: " + "[" * 5000 + "]" * 5000 + "\n"
    assert parse_repo_config(text).state == "invalid"
    assert parse_repo_config("a:" + "\n b:" * 40 + " 1\n").state == "invalid"


def test_an_alias_bomb_is_rejected_without_expanding() -> None:
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for name, previous in zip("bcdefghi", "abcdefgh", strict=True):
        lines.append(f"{name}: &{name} [{', '.join([f'*{previous}'] * 9)}]")
    assert parse_repo_config("\n".join(lines) + "\n").problems == ("anchors",)


def test_the_name_of_an_unknown_key_is_shown_only_if_it_is_plain() -> None:
    assert parse_repo_config("ignore-this: 1\n").problems == ("ignore-this",)
    odd = parse_repo_config("'<script>alert(1)</script>': 1\n")
    assert odd.problems == ("?",)
    assert "script" not in str(odd)


def test_no_value_of_the_file_reaches_the_problems() -> None:
    loaded = parse_repo_config("comment: SECRET-VALUE-9\nllm: SECRET-VALUE-10\n")
    assert "SECRET" not in repr(loaded)


# --- what the caller derives ---------------------------------------------------------------------


def test_an_invalid_file_turns_the_llm_off_and_nothing_else_does() -> None:
    assert effective_llm(parse_repo_config("llm: bogus\n")) == "off"
    assert effective_llm(LoadedConfig(state="unreadable")) == "auto"
    assert effective_llm(LoadedConfig()) == "auto"
    assert effective_llm(valid_file(llm="on_demand")) == "on_demand"
    assert effective_llm(valid_file(llm="off")) == "off"


def test_ignore_workflows_matches_the_exact_name() -> None:
    loaded = valid_file(ignore_workflows=["Nightly"])
    assert ignores(loaded, "Nightly")
    assert not ignores(loaded, "nightly")
    assert not ignores(loaded, "Nightly ")
    assert not ignores(LoadedConfig(), "Nightly")


# --- the cache -----------------------------------------------------------------------------------

REPO = RepoRef(installation_id=1, repo_id=7, full_name="acme/widgets", default_branch="main")


def make_cache() -> tuple[CachedRepoConfig, FakeRepoConfig, FakeClock]:
    clock = FakeClock()
    source = FakeRepoConfig()
    return CachedRepoConfig(source, clock), source, clock


async def test_a_file_is_remembered_for_the_ttl() -> None:
    cache, source, clock = make_cache()
    source.set(valid_file(llm="off"))

    assert await cache.load(REPO) == valid_file(llm="off")
    clock.advance(timedelta(seconds=299))
    source.set(valid_file(llm="auto"))
    assert (await cache.load(REPO)).config.llm == "off"
    assert len(source.calls) == 1

    clock.advance(timedelta(seconds=2))
    assert (await cache.load(REPO)).config.llm == "auto"
    assert len(source.calls) == 2


async def test_a_missing_file_is_remembered_only_for_sixty_seconds() -> None:
    cache, source, clock = make_cache()

    await cache.load(REPO)
    clock.advance(timedelta(seconds=59))
    await cache.load(REPO)
    assert len(source.calls) == 1

    clock.advance(timedelta(seconds=2))
    source.set(valid_file(comment=False))
    assert (await cache.load(REPO)).state == "file"
    assert len(source.calls) == 2


async def test_each_repository_has_its_own_entry() -> None:
    cache, source, _ = make_cache()
    source.set(valid_file(llm="off"), repo_id=7)

    other = REPO.model_copy(update={"repo_id": 8})
    assert (await cache.load(REPO)).config.llm == "off"
    assert (await cache.load(other)).config.llm == "auto"
    assert len(source.calls) == 2


async def test_a_transient_error_is_not_cached_and_not_turned_into_defaults() -> None:
    cache, source, _ = make_cache()
    source.fail_with(TransientError("GitHub is down"), times=1)

    with pytest.raises(TransientError):
        await cache.load(REPO)
    assert (await cache.load(REPO)).state == "default"  # the next call asks again
    assert len(source.calls) == 2


async def test_an_invalid_file_is_remembered_as_long_as_a_valid_one() -> None:
    cache, source, clock = make_cache()
    source.set(parse_repo_config("llm: bogus\n"))

    await cache.load(REPO)
    clock.advance(timedelta(seconds=299))
    assert (await cache.load(REPO)).state == "invalid"
    assert len(source.calls) == 1


def test_the_bare_word_off_is_the_llm_setting_not_a_boolean() -> None:
    # YAML 1.1 turns ``off`` and ``no`` into False; ``on`` and ``yes`` stay invalid for ``llm``.
    assert parse_repo_config("llm: off\n").config.llm == "off"
    assert parse_repo_config("llm: no\n").config.llm == "off"
    assert parse_repo_config("llm: on\n").problems == ("llm",)
