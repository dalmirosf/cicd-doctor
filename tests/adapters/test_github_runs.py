"""The runs of a head commit and the pull request of ``explain`` (D-96), over ``FakeGitHub``."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cicd_doctor.adapters.github.auth import EXPLAIN_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubPermanentError, GitHubTransientError
from cicd_doctor.adapters.github.pulls import PULL, GitHubPullRequests
from cicd_doctor.adapters.github.runs import RUNS, GitHubRunSource
from cicd_doctor.adapters.github.webhooks import route_event
from cicd_doctor.core.models import DiagnoseRunPayload, RepoRef
from tests.factories import T0
from tests.github_fake import (
    CANARY,
    DEFAULT_INSTALLATION_PERMISSIONS,
    INSTALLATION_ID,
    REPO_ID,
    FakeGitHub,
    FakeRepoWrites,
    json_response,
)

SHA = "c" * 40
FORK = "drive-by/widgets"
FIXTURE = Path(__file__).parent.parent / "fixtures" / "github" / "actions_runs_head_sha.json"
REPO = RepoRef(
    installation_id=INSTALLATION_ID,
    repo_id=REPO_ID,
    full_name="acme/widgets",
    default_branch="main",
)


@pytest.fixture
def writes(github: FakeGitHub) -> FakeRepoWrites:
    github.installations[INSTALLATION_ID].permissions["issues"] = "write"  # D-89
    return FakeRepoWrites(github)


@pytest.fixture
def runs(http: GitHubHttp, auth: GitHubAppAuth, writes: FakeRepoWrites) -> GitHubRunSource:
    return GitHubRunSource(http, auth)


@pytest.fixture
def pulls(http: GitHubHttp, auth: GitHubAppAuth, writes: FakeRepoWrites) -> GitHubPullRequests:
    return GitHubPullRequests(http, auth)


# --- GET /pulls/{n} ------------------------------------------------------------------------------


async def test_a_pull_request_of_the_same_repository_is_read(
    pulls: GitHubPullRequests, writes: FakeRepoWrites, github: FakeGitHub
) -> None:
    writes.add_pull(12, head_repo="acme/widgets", head_ref="feature/x", head_sha=SHA)

    pull = await pulls.get(REPO, 12)

    assert pull is not None
    assert (pull.number, pull.state, pull.draft, pull.merged) == (12, "open", False, False)
    assert (pull.head_sha, pull.head_branch) == (SHA, "feature/x")
    assert (pull.head_repo_id, pull.head_repo_full_name) == (REPO_ID, "acme/widgets")
    assert pull.base_repo_id == REPO_ID
    (request,) = github.requests_to("GET", PULL)
    assert request.params == {"owner": "acme", "repo": "widgets", "number": "12"}


async def test_the_pull_request_is_read_with_the_permissions_of_explain(
    pulls: GitHubPullRequests, writes: FakeRepoWrites, github: FakeGitHub
) -> None:
    writes.add_pull(12, head_repo="acme/widgets", head_ref="x", head_sha=SHA)

    await pulls.get(REPO, 12)

    (token,) = github.issued
    assert token.repo_ids == (REPO_ID,)
    assert set(token.permissions.items()) == set(EXPLAIN_PERMISSIONS)


async def test_a_fork_pull_request_names_the_fork_and_a_merged_one_says_so(
    pulls: GitHubPullRequests, writes: FakeRepoWrites
) -> None:
    writes.add_pull(
        13,
        head_repo=FORK,
        head_ref="patch-1",
        head_sha=SHA,
        state="closed",
        merged=True,
        draft=True,
    )

    pull = await pulls.get(REPO, 13)

    assert pull is not None
    assert (pull.state, pull.merged, pull.draft) == ("closed", True, True)
    assert pull.head_repo_full_name == FORK
    assert pull.head_repo_id == 7777


async def test_a_pull_request_whose_fork_was_deleted_has_no_head_repository(
    pulls: GitHubPullRequests, writes: FakeRepoWrites
) -> None:
    writes.add_pull(14, head_repo="", head_ref="patch-1", head_sha=SHA)

    pull = await pulls.get(REPO, 14)

    assert pull is not None
    assert (pull.head_repo_id, pull.head_repo_full_name) == (None, "")


async def test_a_missing_pull_request_is_none(pulls: GitHubPullRequests) -> None:
    assert await pulls.get(REPO, 404) is None


async def test_a_5xx_reading_the_pull_request_is_transient_and_hides_the_body(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    github.fail("GET", PULL, 502)

    with pytest.raises(GitHubTransientError) as caught:
        await pulls.get(REPO, 12)

    assert CANARY not in str(caught.value)


async def test_a_rate_limit_reading_the_pull_request_is_transient(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    github.fail("GET", PULL, 429, headers={"retry-after": "7"})

    with pytest.raises(GitHubTransientError) as caught:
        await pulls.get(REPO, 12)

    assert caught.value.retry_after == timedelta(seconds=7)


async def test_a_missing_permission_reading_the_pull_request_is_permanent(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    github.fail("GET", PULL, 403, message="Resource not accessible by integration")

    with pytest.raises(GitHubPermanentError):
        await pulls.get(REPO, 12)


async def test_an_answer_of_another_shape_is_a_permanent_error(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    github.add_route("GET", PULL, lambda request: json_response(200, {"number": "x"}))

    with pytest.raises(Exception, match="unexpected shape"):
        await pulls.get(REPO, 12)


# --- GET /actions/runs?head_sha= -----------------------------------------------------------------


async def test_the_query_carries_the_head_sha_and_one_page_of_100(
    runs: GitHubRunSource, writes: FakeRepoWrites, github: FakeGitHub
) -> None:
    writes.add_run(10, head_sha=SHA)
    writes.add_run(11, head_sha="d" * 40)

    listed = await runs.runs_for_head(REPO, SHA)

    assert [item.run.run_id for item in listed] == [10]
    (request,) = github.requests_to("GET", RUNS)
    assert request.query == {"head_sha": SHA, "per_page": "100"}
    (token,) = github.issued
    assert set(token.permissions.items()) == set(EXPLAIN_PERMISSIONS)


async def test_only_the_first_page_is_read_even_when_there_is_a_next_one(
    runs: GitHubRunSource, writes: FakeRepoWrites, github: FakeGitHub
) -> None:
    writes.page_cap = 2
    for run_id in (1, 2, 3, 4, 5):
        writes.add_run(run_id, head_sha=SHA, workflow_id=run_id)

    listed = await runs.runs_for_head(REPO, SHA)

    assert [item.run.run_id for item in listed] == [5, 4]  # the newest first, and no page 2
    assert len(github.requests_to("GET", RUNS)) == 1


async def test_a_run_of_a_fork_is_a_fork_run_with_no_pull_request_number(
    runs: GitHubRunSource, writes: FakeRepoWrites
) -> None:
    writes.add_run(20, head_sha=SHA, head_repo=FORK, head_branch="patch-1", pull_requests=[])

    (item,) = await runs.runs_for_head(REPO, SHA)

    assert item.run.is_fork is True
    assert item.run.pr_number is None
    assert item.run.head_repo_full_name == FORK
    assert item.head_repo_id == 7777


async def test_a_run_of_the_repository_carries_its_own_pull_request_number(
    runs: GitHubRunSource, writes: FakeRepoWrites
) -> None:
    writes.add_run(21, head_sha=SHA, pull_requests=[12])

    (item,) = await runs.runs_for_head(REPO, SHA)

    assert (item.run.is_fork, item.run.pr_number) == (False, 12)


async def test_a_run_whose_head_repository_was_deleted_is_a_fork(
    runs: GitHubRunSource, writes: FakeRepoWrites
) -> None:
    writes.add_run(22, head_sha=SHA, head_repo="")

    (item,) = await runs.runs_for_head(REPO, SHA)

    assert item.run.is_fork is True
    assert item.run.head_repo_full_name == ""
    assert item.head_repo_id is None


async def test_the_status_and_the_conclusion_are_kept_as_listed(
    runs: GitHubRunSource, writes: FakeRepoWrites
) -> None:
    writes.add_run(30, head_sha=SHA, workflow_id=1, status="in_progress")
    writes.add_run(31, head_sha=SHA, workflow_id=2, conclusion="action_required")

    listed = {item.run.run_id: item for item in await runs.runs_for_head(REPO, SHA)}

    assert (listed[30].status, listed[30].conclusion) == ("in_progress", None)
    assert (listed[31].status, listed[31].conclusion) == ("completed", "action_required")


async def test_an_item_of_an_unexpected_shape_is_skipped_and_the_rest_is_kept(
    runs: GitHubRunSource, github: FakeGitHub, writes: FakeRepoWrites
) -> None:
    writes.add_run(40, head_sha=SHA)
    good = writes.runs[0]
    body = {"total_count": 2, "workflow_runs": [{"id": "x"}, {**good}]}
    github.add_route("GET", RUNS, lambda request: json_response(200, body))

    listed = await runs.runs_for_head(REPO, SHA)

    assert [item.run.run_id for item in listed] == [40]


@pytest.mark.parametrize("body", [[], {"workflow_runs": None}, {"total_count": 0}])
async def test_a_body_without_a_list_of_runs_is_an_empty_listing(
    runs: GitHubRunSource, github: FakeGitHub, body: Any
) -> None:
    github.add_route("GET", RUNS, lambda request: json_response(200, body))

    assert await runs.runs_for_head(REPO, SHA) == []


async def test_a_5xx_listing_the_runs_is_transient_and_hides_the_body(
    runs: GitHubRunSource, github: FakeGitHub
) -> None:
    github.fail("GET", RUNS, 503)

    with pytest.raises(GitHubTransientError) as caught:
        await runs.runs_for_head(REPO, SHA)

    assert CANARY not in str(caught.value)


async def test_a_rate_limit_listing_the_runs_is_transient(
    runs: GitHubRunSource, github: FakeGitHub
) -> None:
    github.fail("GET", RUNS, 403, headers={"x-ratelimit-remaining": "0", "retry-after": "9"})

    with pytest.raises(GitHubTransientError):
        await runs.runs_for_head(REPO, SHA)


async def test_a_404_listing_the_runs_is_permanent(
    runs: GitHubRunSource, github: FakeGitHub
) -> None:
    github.fail("GET", RUNS, 404)

    with pytest.raises(GitHubPermanentError):
        await runs.runs_for_head(REPO, SHA)


# --- the recorded item of the real API (spike of P3, B1) -----------------------------------------


async def test_a_real_item_gives_the_same_run_ref_as_the_webhook(
    runs: GitHubRunSource, github: FakeGitHub
) -> None:
    recorded = json.loads(FIXTURE.read_text("utf-8"))
    item = recorded["workflow_runs"][0]
    repo = RepoRef(
        installation_id=INSTALLATION_ID,
        repo_id=item["repository"]["id"],
        full_name=item["repository"]["full_name"],
        default_branch="main",
    )
    github.install(
        INSTALLATION_ID,
        repo_ids={repo.repo_id},
        permissions={**DEFAULT_INSTALLATION_PERMISSIONS, "issues": "write"},
    )
    github.add_route("GET", RUNS, lambda request: json_response(200, recorded))
    envelope = {
        "action": "completed",
        "workflow_run": item,
        "repository": {**item["repository"], "default_branch": "main", "private": True},
        "installation": {"id": INSTALLATION_ID},
    }
    job = route_event("workflow_run", envelope, received_at=T0, max_attempts=5)
    assert job is not None
    from_webhook = DiagnoseRunPayload.model_validate(job.payload).run

    (summary,) = await runs.runs_for_head(repo, item["head_sha"])

    assert summary.run == from_webhook
    assert (summary.status, summary.conclusion) == ("completed", "failure")
    assert summary.head_repo_id == item["head_repository"]["id"]
