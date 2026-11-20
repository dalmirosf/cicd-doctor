"""``GitHubRepoConfigSource`` over ``FakeGitHub`` (D-90): the default branch only, the token it
asks for, and what each answer of the ``contents`` endpoint means."""

import base64
import logging

import pytest

from cicd_doctor.adapters.github.auth import CONFIG_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubTransientError
from cicd_doctor.adapters.github.repo_config import CONFIG_FILE, GitHubRepoConfigSource
from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import RepoRef
from cicd_doctor.core.repo_config import MAX_CONFIG_BYTES
from tests.github_fake import CANARY, INSTALLATION_ID, REPO_ID, FakeGitHub, FakeRepoContents

REPO = RepoRef(
    installation_id=INSTALLATION_ID,
    repo_id=REPO_ID,
    full_name="acme/widgets",
    default_branch="main",
)


@pytest.fixture
def contents(github: FakeGitHub) -> FakeRepoContents:
    return FakeRepoContents(github)


@pytest.fixture
def source(
    http: GitHubHttp, auth: GitHubAppAuth, contents: FakeRepoContents
) -> GitHubRepoConfigSource:
    return GitHubRepoConfigSource(http, auth)


async def test_a_valid_file_is_read(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put("comment: false\nllm: on_demand\nignore_workflows: [Nightly]\n")

    loaded = await source.load(REPO)

    assert loaded.state == "file"
    assert (loaded.config.comment, loaded.config.llm) == (False, "on_demand")
    assert loaded.config.ignore_workflows == ["Nightly"]


async def test_no_file_is_the_defaults(source: GitHubRepoConfigSource) -> None:
    loaded = await source.load(REPO)

    assert loaded.state == "default"
    assert loaded.config.comment is True
    assert loaded.config.llm == "auto"


# --- the default branch only ---------------------------------------------------------------------


async def test_no_request_ever_carries_a_ref(
    source: GitHubRepoConfigSource, contents: FakeRepoContents, github: FakeGitHub
) -> None:
    contents.put("llm: auto\n")
    await source.load(REPO)
    await source.load(REPO.model_copy(update={"default_branch": "develop"}))

    requests = github.requests_to("GET", CONFIG_FILE)
    assert len(requests) == 2
    for request in requests:
        assert request.raw_query == ""
        assert "ref" not in request.query
        assert request.path.endswith("/contents/.github/cicd-doctor.yml")


async def test_a_file_on_a_pull_request_head_changes_nothing(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put("comment: true\n")
    contents.put("llm: off\ncomment: false\nignore_workflows: [CI]\n", ref="fork-head-sha")
    contents.put("llm: off\n", ref="feature")

    loaded = await source.load(REPO)

    assert loaded.config.llm == "auto"
    assert loaded.config.comment is True
    assert loaded.config.ignore_workflows == []


async def test_a_file_only_on_a_pull_request_head_is_not_found(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put("llm: off\n", ref="fork-head-sha")

    assert (await source.load(REPO)).state == "default"


def test_the_source_has_no_way_to_ask_for_a_ref() -> None:
    import inspect

    assert list(inspect.signature(GitHubRepoConfigSource.load).parameters) == ["self", "repo"]
    assert "{ref}" not in CONFIG_FILE
    assert "ref" not in CONFIG_FILE.replace("/repos", "")


# --- the token -----------------------------------------------------------------------------------


async def test_the_token_has_contents_read_and_nothing_else(
    source: GitHubRepoConfigSource, contents: FakeRepoContents, github: FakeGitHub
) -> None:
    contents.put("llm: auto\n")

    await source.load(REPO)

    (token,) = github.issued
    assert token.repo_ids == (REPO_ID,)
    assert set(token.permissions.items()) == set(CONFIG_PERMISSIONS) == {("contents", "read")}
    assert github.requests_to("GET", CONFIG_FILE)[0].has_bearer(token.token)


async def test_an_installation_that_did_not_accept_contents_read_uses_the_defaults(
    source: GitHubRepoConfigSource, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    github.install(permissions={"actions": "read", "checks": "write", "pull_requests": "write"})

    with caplog.at_level(logging.WARNING):
        loaded = await source.load(REPO)

    assert loaded.state == "unreadable"
    assert loaded.config.llm == "auto"
    assert any("cannot be read" in record.message for record in caplog.records)
    assert CANARY not in caplog.text


async def test_a_403_on_the_file_is_unreadable_and_leaks_no_body(
    source: GitHubRepoConfigSource, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    github.fail("GET", CONFIG_FILE, 403, message="Resource not accessible by integration")

    with caplog.at_level(logging.WARNING):
        loaded = await source.load(REPO)

    assert loaded.state == "unreadable"
    assert CANARY not in caplog.text


async def test_a_deleted_installation_is_not_taken_for_a_missing_file(
    source: GitHubRepoConfigSource, github: FakeGitHub
) -> None:
    github.installations.clear()

    with pytest.raises(PermanentError):
        await source.load(REPO)


# --- errors that must retry ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_is_transient_and_not_the_defaults(
    source: GitHubRepoConfigSource, github: FakeGitHub, status: int
) -> None:
    github.fail("GET", CONFIG_FILE, status)

    with pytest.raises(GitHubTransientError) as caught:
        await source.load(REPO)
    assert CANARY not in str(caught.value)


async def test_a_rate_limit_is_transient(
    source: GitHubRepoConfigSource, github: FakeGitHub
) -> None:
    github.fail("GET", CONFIG_FILE, 429, headers={"retry-after": "30"})

    with pytest.raises(GitHubTransientError) as caught:
        await source.load(REPO)
    assert CANARY not in str(caught.value)


async def test_a_timeout_is_transient(source: GitHubRepoConfigSource, github: FakeGitHub) -> None:
    import httpx2

    github.fail("GET", CONFIG_FILE, error=httpx2.ReadTimeout("slow"))

    with pytest.raises(GitHubTransientError):
        await source.load(REPO)


# --- what is not a plain file --------------------------------------------------------------------


async def test_a_directory_is_invalid(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put_raw([{"type": "file", "name": "a.yml"}])

    loaded = await source.load(REPO)

    assert (loaded.state, loaded.problems) == ("invalid", ("response",))


@pytest.mark.parametrize("kind", ["dir", "symlink", "submodule"])
async def test_a_type_other_than_file_is_invalid(
    source: GitHubRepoConfigSource, contents: FakeRepoContents, kind: str
) -> None:
    contents.put_raw({"type": kind, "size": 10, "encoding": "base64", "content": ""})

    assert (await source.load(REPO)).problems == ("type",)


async def test_a_file_over_the_cap_is_invalid_without_decoding_it(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put_raw(
        {"type": "file", "size": MAX_CONFIG_BYTES + 1, "encoding": "base64", "content": "!!!"}
    )

    assert (await source.load(REPO)).problems == ("size",)


async def test_a_declared_size_that_lies_is_caught_on_the_decoded_bytes(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    big = base64.b64encode(b"#" * (MAX_CONFIG_BYTES + 5)).decode()
    contents.put_raw({"type": "file", "size": 10, "encoding": "base64", "content": big})

    assert (await source.load(REPO)).problems == ("size",)


@pytest.mark.parametrize(
    "body",
    [
        {"type": "file", "size": 10, "encoding": "none", "content": ""},  # 1 MB+ on the real API
        {"type": "file", "size": 10, "encoding": "utf-8", "content": "llm: off"},
        {"type": "file", "size": 10, "encoding": "base64", "content": 5},
        {"type": "file", "size": 10, "encoding": "base64"},
    ],
)
async def test_another_encoding_is_invalid(
    source: GitHubRepoConfigSource, contents: FakeRepoContents, body: dict[str, object]
) -> None:
    contents.put_raw(body)

    assert (await source.load(REPO)).problems == ("encoding",)


async def test_bytes_that_are_not_utf8_are_invalid(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put(b"llm: \xff\xfe\n")

    assert (await source.load(REPO)).problems == ("encoding",)


async def test_a_file_without_a_size_is_invalid(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put_raw({"type": "file", "encoding": "base64", "content": ""})

    assert (await source.load(REPO)).problems == ("size",)


async def test_an_invalid_file_names_only_fields(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put("llm: SECRET-TOKEN-VALUE\nextra: 1\n")

    loaded = await source.load(REPO)

    assert loaded.state == "invalid"
    assert loaded.problems == ("llm", "extra")
    assert "SECRET" not in repr(loaded)


async def test_an_empty_file_is_the_defaults(
    source: GitHubRepoConfigSource, contents: FakeRepoContents
) -> None:
    contents.put("")

    assert (await source.load(REPO)).state == "default"
