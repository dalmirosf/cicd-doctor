"""The pull request of a run (D-72): the number the run carries, else a search by head."""

import pytest

from cicd_doctor.adapters.github.auth import DIAGNOSIS_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubTransientError
from cicd_doctor.adapters.github.pulls import PULLS, GitHubPullRequests
from cicd_doctor.core.models import RunRef
from tests.factories import make_run_ref
from tests.github_fake import CANARY, INSTALLATION_ID, REPO_ID, FakeGitHub, FakeRepoWrites

SHA = "c" * 40
FORK = "drive-by/widgets"


def fork_run(**changes: object) -> RunRef:
    fields: dict[str, object] = {
        "installation_id": INSTALLATION_ID,
        "repo_id": REPO_ID,
        "repo_full_name": "acme/widgets",
        "head_sha": SHA,
        "head_branch": "fix-typo",
        "head_repo_full_name": FORK,
        "event": "pull_request",
        "is_fork": True,
        "pr_number": None,
    }
    return make_run_ref(**(fields | changes))


@pytest.fixture
def repo(github: FakeGitHub) -> FakeRepoWrites:
    return FakeRepoWrites(github)


@pytest.fixture
def pulls(http: GitHubHttp, auth: GitHubAppAuth, repo: FakeRepoWrites) -> GitHubPullRequests:
    return GitHubPullRequests(http, auth)


async def test_the_number_the_run_carries_is_used_without_any_request(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    assert await pulls.find(fork_run(pr_number=12)) == 12
    assert github.requests == []


async def test_a_fork_pull_request_is_found_by_its_head(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.add_pull(34, head_repo=FORK, head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run()) == 34

    (request,) = github.requests_to("GET", PULLS)
    assert request.query["head"] == "drive-by:fix-typo"
    assert request.query["state"] == "open"
    assert request.params == {"owner": "acme", "repo": "widgets"}


async def test_the_search_uses_the_diagnosis_token_of_the_repository(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    await pulls.find(fork_run())

    (token,) = github.issued
    assert token.repo_ids == (REPO_ID,)
    assert set(token.permissions.items()) == set(DIAGNOSIS_PERMISSIONS)
    assert github.requests_to("GET", PULLS)[0].has_bearer(token.token)


async def test_a_pull_request_with_another_head_commit_is_not_this_run(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull(34, head_repo=FORK, head_ref="fix-typo", head_sha="d" * 40)

    assert await pulls.find(fork_run()) is None


async def test_a_pull_request_from_another_repository_of_the_same_owner_is_not_this_run(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull(34, head_repo="drive-by/other-project", head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run()) is None


async def test_a_pull_request_of_another_branch_or_owner_is_not_listed(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull(34, head_repo=FORK, head_ref="other-branch", head_sha=SHA)
    repo.add_pull(35, head_repo="someone/widgets", head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run()) is None


async def test_a_closed_pull_request_is_not_the_one(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull(34, head_repo=FORK, head_ref="fix-typo", head_sha=SHA, state="closed")

    assert await pulls.find(fork_run()) is None


async def test_several_matches_give_the_lowest_number(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    for number in (41, 17, 29):
        repo.add_pull(number, head_repo=FORK, head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run()) == 17


async def test_the_search_pages_through_the_listing(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.page_cap = 1
    repo.add_pull(10, head_repo=FORK, head_ref="fix-typo", head_sha="d" * 40)
    repo.add_pull(11, head_repo=FORK, head_ref="fix-typo", head_sha="e" * 40)
    repo.add_pull(12, head_repo=FORK, head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run()) == 12
    assert len(github.requests_to("GET", PULLS)) == 3


async def test_a_deleted_fork_has_no_pull_request_and_nothing_is_searched(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    assert await pulls.find(fork_run(head_repo_full_name="")) is None
    assert github.requests == []


async def test_a_run_without_a_head_branch_has_no_search(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    assert await pulls.find(fork_run(head_branch=None)) is None
    assert github.requests == []


async def test_a_pull_request_event_of_the_same_repository_is_searched_too(
    pulls: GitHubPullRequests, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    """``pull_requests`` lists a PR of another repository for the same branch name: the webhook
    drops it, so the run arrives without a number although the PR exists here."""
    repo.add_pull(8, head_repo="acme/widgets", head_ref="feature/x", head_sha=SHA)
    run = fork_run(is_fork=False, head_repo_full_name="acme/widgets", head_branch="feature/x")

    assert await pulls.find(run) == 8
    assert github.requests_to("GET", PULLS)[0].query["head"] == "acme:feature/x"


async def test_a_pull_request_target_event_is_searched(
    pulls: GitHubPullRequests, repo: FakeRepoWrites
) -> None:
    repo.add_pull(9, head_repo=FORK, head_ref="fix-typo", head_sha=SHA)

    assert await pulls.find(fork_run(event="pull_request_target")) == 9


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
async def test_a_run_that_is_not_from_a_pull_request_has_none_and_nothing_is_searched(
    pulls: GitHubPullRequests, github: FakeGitHub, event: str
) -> None:
    run = fork_run(is_fork=False, head_repo_full_name="acme/widgets", event=event)

    assert await pulls.find(run) is None
    assert github.requests == []


async def test_a_failing_listing_raises_without_the_body_of_the_response(
    pulls: GitHubPullRequests, github: FakeGitHub
) -> None:
    github.fail("GET", PULLS, 500, times=1)

    with pytest.raises(GitHubTransientError) as caught:
        await pulls.find(fork_run())

    assert CANARY not in str(caught.value)
