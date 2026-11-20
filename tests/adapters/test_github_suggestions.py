"""The diff of a pull request and the review comment of a suggestion, on FakeGitHub (D-97).

What GitHub answers to a review comment on a line outside the diff was NOT measured (it needs a
write on a real pull request): FakeGitHub answers 422 like the documentation says, and the reporter
treats ANY 422 as "text only". The live check of P3 (g) records the real answer.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import DIAGNOSIS_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubPermanentError
from cicd_doctor.adapters.github.pulls import PULL, PULL_FILES, GitHubPullRequests
from cicd_doctor.adapters.github.reporter import PULL_COMMENT, PULL_COMMENTS, GitHubReporter
from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.diagnosis.prompt import build_messages
from cicd_doctor.core.diff import MAX_TOKENS, select_for_prompt
from cicd_doctor.core.errors import PermanentError, TransientError
from cicd_doctor.core.models import (
    CommentTarget,
    DiffFile,
    PullDiff,
    RepoRef,
    ReviewSuggestion,
    Suggestion,
)
from cicd_doctor.core.render import render_review_comment
from cicd_doctor.core.suggestions import suggestion_marker
from tests.diffs import patch_of
from tests.factories import make_run_ref
from tests.github_fake import (
    APP_SLUG,
    CANARY,
    INSTALLATION_ID,
    REPO_ID,
    FakeGitHub,
    FakeRepoWrites,
)
from tests.llmcases import make_excerpt, make_request

FIXTURES = Path(__file__).parents[1] / "fixtures" / "github"
SHA = "a" * 40
PR = 12
BOT = f"{APP_SLUG}[bot]"
REPO = RepoRef(
    installation_id=INSTALLATION_ID,
    repo_id=REPO_ID,
    full_name="acme/widgets",
    default_branch="main",
)
FAILURE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def repo(github: FakeGitHub) -> FakeRepoWrites:
    return FakeRepoWrites(github)


@pytest.fixture
def pulls(http: GitHubHttp, auth: GitHubAppAuth, repo: FakeRepoWrites) -> GitHubPullRequests:
    return GitHubPullRequests(http, auth)


@pytest.fixture
def reporter(
    http: GitHubHttp, auth: GitHubAppAuth, clock: FakeClock, repo: FakeRepoWrites
) -> GitHubReporter:
    return GitHubReporter(http, auth, clock)


def file_item(name: str, patch: str | None, **changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"sha": "0" * 40, "filename": name, "status": "modified"}
    if patch is not None:
        item["patch"] = patch
    return item | changes


# --- GET /pulls/{n}/files -------------------------------------------------------------------


async def test_the_real_files_listing_is_read_with_its_patches(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    real = json.loads((FIXTURES / "pulls_files_pr36.json").read_text(encoding="utf-8"))
    repo.add_pull_files(PR, real)

    diff = await pulls.diff(REPO, PR)

    assert [f.filename for f in diff.files] == [item["filename"] for item in real]
    assert [f.patch for f in diff.files] == [item["patch"] for item in real]
    assert diff.truncated is False
    (request,) = github.requests_to("GET", PULL_FILES)
    assert request.params == {"owner": "acme", "repo": "widgets", "number": str(PR)}
    assert request.query["per_page"] == "100"
    (token,) = github.issued
    assert set(token.permissions.items()) == set(DIAGNOSIS_PERMISSIONS)  # no Issues: write needed


async def test_a_file_without_patch_and_a_renamed_one_keep_what_github_gave(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull_files(
        PR,
        [
            file_item("uv.lock", None),
            file_item("new.py", None, status="renamed", previous_filename="old.py"),
            file_item("gone.py", None, status="removed"),
        ],
    )

    diff = await pulls.diff(REPO, PR)

    assert [(f.filename, f.status, f.previous_filename, f.patch) for f in diff.files] == [
        ("uv.lock", "modified", None, None),
        ("new.py", "renamed", "old.py", None),
        ("gone.py", "removed", None, None),
    ]


async def test_a_long_listing_is_followed_page_by_page(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.page_cap = 2
    repo.add_pull_files(PR, [file_item(f"f{i}.py", "@@ -1 +1 @@\n-a\n+b") for i in range(5)])

    diff = await pulls.diff(REPO, PR)

    assert [f.filename for f in diff.files] == [f"f{i}.py" for i in range(5)]
    assert len(github.requests_to("GET", PULL_FILES)) == 3


async def test_a_listing_longer_than_the_cap_is_marked_truncated(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cicd_doctor.adapters.github.pulls.FILES_PAGES", 2)
    repo.page_cap = 1
    repo.add_pull_files(PR, [file_item(f"f{i}.py", None) for i in range(5)])

    diff = await pulls.diff(REPO, PR)

    assert diff.truncated is True
    assert len(diff.files) == 2


async def test_an_unexpected_item_is_skipped_and_a_missing_pull_request_is_empty(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull_files(PR, [{"surprise": 1}, file_item("ok.py", None), "text"])  # type: ignore[list-item]

    assert [f.filename for f in (await pulls.diff(REPO, PR)).files] == ["ok.py"]
    assert await pulls.diff(REPO, 999) == PullDiff()  # 404


async def test_server_errors_and_permission_errors_are_not_swallowed(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.add_pull_files(PR, [])
    github.fail("GET", PULL_FILES, 502, times=1)
    with pytest.raises(TransientError):
        await pulls.diff(REPO, PR)
    github.fail("GET", PULL_FILES, 403, message="Resource not accessible", times=1)
    with pytest.raises(PermanentError):
        await pulls.diff(REPO, PR)


async def test_the_pull_request_can_be_read_with_the_diagnosis_permissions_only(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.add_pull(PR, head_repo="acme/widgets", head_ref="x", head_sha=SHA)

    pull = await pulls.get(REPO, PR, for_explain=False)

    assert pull is not None
    assert pull.head_sha == SHA
    (token,) = github.issued
    assert set(token.permissions.items()) == set(DIAGNOSIS_PERMISSIONS)
    assert len(github.requests_to("GET", PULL)) == 1


# --- review comments ------------------------------------------------------------------------

DIFF_PATCH = patch_of((1, 1, [f" line {n}" for n in range(1, 21)]))


def make_review(**changes: Any) -> ReviewSuggestion:
    suggestion = Suggestion(path="src/a.py", start_line=3, end_line=5, replacement="x = 1")
    fields: dict[str, Any] = {
        "pr_number": PR,
        "commit_id": SHA,
        "path": "src/a.py",
        "start_line": 3,
        "end_line": 5,
        "body": render_review_comment(PR, UUID(FAILURE), "Pin the version.", suggestion),
    }
    return ReviewSuggestion(**(fields | changes))


TARGET = CommentTarget(
    run=make_run_ref(installation_id=INSTALLATION_ID, repo_id=REPO_ID), pr_number=PR
)


@pytest.fixture
def with_files(repo: FakeRepoWrites) -> FakeRepoWrites:
    repo.add_pull_files(PR, [file_item("src/a.py", DIFF_PATCH)])
    return repo


async def test_a_multi_line_suggestion_is_posted_on_the_right_side(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    review = make_review()

    assert await reporter.post_suggestion(TARGET, review) is True

    (request,) = github.requests_to("POST", PULL_COMMENTS)
    assert request.json == {
        "body": review.body,
        "commit_id": SHA,
        "path": "src/a.py",
        "line": 5,
        "side": "RIGHT",
        "start_line": 3,
        "start_side": "RIGHT",
    }
    assert request.params["pull_number"] == str(PR)
    [stored] = with_files.review_comments.values()
    assert stored["body"].split("\n")[0] == suggestion_marker(PR, FAILURE)
    assert stored["user"]["login"] == BOT


async def test_a_single_line_suggestion_has_no_start_line(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    await reporter.post_suggestion(TARGET, make_review(start_line=4, end_line=4))

    (request,) = github.requests_to("POST", PULL_COMMENTS)
    assert request.json["line"] == 4
    assert "start_line" not in request.json
    assert "start_side" not in request.json


async def test_posting_twice_edits_the_comment_of_the_bot(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    await reporter.post_suggestion(TARGET, make_review())
    newer = make_review(body=make_review().body + "\nmore")

    assert await reporter.post_suggestion(TARGET, newer) is True

    assert len(with_files.review_comments) == 1
    assert len(github.requests_to("POST", PULL_COMMENTS)) == 1
    (edit,) = github.requests_to("PATCH", PULL_COMMENT)
    assert edit.json == {"body": newer.body}
    [stored] = with_files.review_comments.values()
    assert stored["body"] == newer.body


async def test_a_comment_of_somebody_else_with_our_marker_is_never_edited(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    marker = suggestion_marker(PR, FAILURE)
    forged = with_files.add_bot_review_comment(PR, marker + "\nforged", login="mallory")

    assert await reporter.post_suggestion(TARGET, make_review()) is True

    assert github.requests_to("PATCH", PULL_COMMENT) == []
    assert with_files.review_comments[forged["id"]]["body"].endswith("forged")
    assert len(with_files.review_comments) == 2


async def test_a_marker_of_another_failure_does_not_match(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    with_files.add_bot_review_comment(PR, suggestion_marker(PR, "0" * 8 + FAILURE[8:]) + "\nx")

    await reporter.post_suggestion(TARGET, make_review())

    assert github.requests_to("PATCH", PULL_COMMENT) == []
    assert len(with_files.review_comments) == 2


async def test_a_comment_that_vanished_between_the_listing_and_the_edit_is_created(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    with_files.add_bot_review_comment(PR, suggestion_marker(PR, FAILURE) + "\nold")
    github.fail("PATCH", PULL_COMMENT, 404, message="Not Found", times=1)

    assert await reporter.post_suggestion(TARGET, make_review()) is True

    assert len(github.requests_to("POST", PULL_COMMENTS)) == 1


async def test_a_line_outside_the_diff_is_text_only_not_an_error(
    reporter: GitHubReporter, with_files: FakeRepoWrites
) -> None:
    assert await reporter.post_suggestion(TARGET, make_review(start_line=30, end_line=31)) is False
    assert with_files.review_comments == {}


@pytest.mark.parametrize("status", [422])
async def test_any_422_is_text_only(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub, status: int
) -> None:
    github.fail("POST", PULL_COMMENTS, status, message="whatever the real message is", times=1)

    assert await reporter.post_suggestion(TARGET, make_review()) is False


async def test_a_missing_permission_names_the_permission_and_keeps_the_status(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    github.fail("POST", PULL_COMMENTS, 403, message="Resource not accessible", times=1)

    with pytest.raises(GitHubPermanentError) as caught:
        await reporter.post_suggestion(TARGET, make_review())

    assert caught.value.status_code == 403
    assert "Pull requests: write" in str(caught.value)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_is_transient(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub, status: int
) -> None:
    github.fail("POST", PULL_COMMENTS, status, times=1)

    with pytest.raises(TransientError):
        await reporter.post_suggestion(TARGET, make_review())


@pytest.mark.parametrize(
    "body",
    [
        "no marker",
        "<!-- cicd-doctor:v1 suggestion pr=99 failure=" + FAILURE + " -->\nx",  # another PR
        "<!-- cicd-doctor:v1 suggestion pr=12 failure=not-a-uuid -->\nx",
        "\n<!-- cicd-doctor:v1 suggestion pr=12 failure=" + FAILURE + " -->",  # not the first line
        "<!-- cicd-doctor:v1 pr=12 workflow=7 -->\nx",  # the sticky marker
    ],
)
async def test_a_body_without_its_own_marker_is_refused_before_any_request(
    reporter: GitHubReporter, github: FakeGitHub, body: str
) -> None:
    with pytest.raises(PermanentError):
        await reporter.post_suggestion(TARGET, make_review(body=body))

    assert github.requests_to("POST", PULL_COMMENTS) == []


async def test_the_token_is_the_one_of_the_diagnosis(
    reporter: GitHubReporter, with_files: FakeRepoWrites, github: FakeGitHub
) -> None:
    await reporter.post_suggestion(TARGET, make_review())

    (token,) = github.issued
    assert set(token.permissions.items()) == set(DIAGNOSIS_PERMISSIONS)
    assert token.repo_ids == (REPO_ID,)


# --- the size of the prompt, with the real tokenizer ------------------------------------------


def test_the_diff_block_stays_within_its_token_bound_with_the_real_tokenizer() -> None:
    counter = TiktokenCounter()
    docs = ["+" + ("Documentation lines are very long, with many words in them. " * 12)] * 40
    code = [f"+    value_{n} = compute(argument_{n}, other_{n}) # note" for n in range(300)]
    diff = PullDiff(
        files=[
            DiffFile(filename="docs/guide.md", status="modified", patch=patch_of((1, 1, docs))),
            DiffFile(filename="src/app/cli.py", status="modified", patch=patch_of((1, 1, code))),
        ]
    )
    excerpt = make_excerpt(["docs/guide.md:3: bad", "File src/app/cli.py:7: boom"])

    shown = select_for_prompt(diff, excerpt, counter)

    assert shown is not None
    assert shown.token_count <= MAX_TOKENS
    request = make_request(excerpt, diff=shown)
    block = build_messages(request, "0123456789abcdef").user.split("<untrusted_diff")[1]
    block = block.split("</untrusted_diff")[0]
    assert counter.count(block) <= MAX_TOKENS + 80  # the nonce tag and the "@@" markers
    # and the whole prompt is a sane size for one call
    whole = build_messages(request, "0123456789abcdef")
    assert counter.count(whole.system + whole.user) < 9_000
