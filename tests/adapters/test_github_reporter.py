"""The GitHub ``Reporter`` (D-70, D-76): the check run and the sticky comment, on FakeGitHub.

Nobody has verified yet whether GitHub lets a completed check run go back to ``in_progress`` (U10),
so the check run tests run against both behaviours of the fake (``allow_reopen``).
"""

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import DIAGNOSIS_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import (
    GitHubHttp,
    GitHubPermanentError,
    GitHubTransientError,
)
from cicd_doctor.adapters.github.reporter import (
    CHECK_RUN,
    CHECK_RUNS,
    COMMIT_CHECK_RUNS,
    ISSUE_COMMENT,
    ISSUE_COMMENTS,
    GitHubReporter,
    comment_marker,
    external_id_for,
)
from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import (
    Category,
    CheckOutput,
    CommentTarget,
    Diagnosis,
    DiagnosisReport,
    Evidence,
    RunRef,
)
from cicd_doctor.core.render import MAX_COMMENT_CHARS, check_name, render_comment
from tests.factories import make_failure, make_run_ref
from tests.github_fake import (
    APP_ID,
    APP_SLUG,
    CANARY,
    INSTALLATION_ID,
    MAX_CHECK_SUMMARY,
    REPO_ID,
    FakeGitHub,
    FakeRepoWrites,
)

SHA = "a" * 40
BOT = f"{APP_SLUG}[bot]"
PR = 12
WORKFLOW = 7
MARKER = comment_marker(PR, WORKFLOW)


def make_run(**changes: object) -> RunRef:
    fields: dict[str, object] = {
        "installation_id": INSTALLATION_ID,
        "repo_id": REPO_ID,
        "repo_full_name": "acme/widgets",
        "head_sha": SHA,
        "workflow_id": WORKFLOW,
        "workflow_name": "CI",
        "pr_number": PR,
    }
    return make_run_ref(**(fields | changes))


RUN = make_run()
TARGET = CommentTarget(run=RUN, pr_number=PR)
OUTPUT = CheckOutput(title="Likely cause: dependency (high confidence)", summary="## details")


@pytest.fixture
def repo(github: FakeGitHub) -> FakeRepoWrites:
    return FakeRepoWrites(github)


@pytest.fixture
def reporter(
    http: GitHubHttp, auth: GitHubAppAuth, clock: FakeClock, repo: FakeRepoWrites
) -> GitHubReporter:
    return GitHubReporter(http, auth, clock)


def comment_body(text: str = "details", marker: str = MARKER) -> str:
    return f"{marker}\n### CICD Doctor: CI\n{text}"


# --- the check run: start ---------------------------------------------------------------------


async def test_a_new_check_run_is_created_in_progress_with_its_external_id(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    check_id = await reporter.start_check(RUN, None)

    created = repo.check_runs[check_id]
    assert created["name"] == "CICD Doctor: CI"
    assert created["head_sha"] == SHA
    assert created["status"] == "in_progress"
    assert created["external_id"] == f"cicd-doctor:run:{RUN.run_id}" == external_id_for(5000)
    assert created["details_url"] == f"https://github.com/acme/widgets/actions/runs/{RUN.run_id}"
    assert created["output"]["summary"] == "Analyzing failure… (attempt 1)"
    assert len(github.requests_to("POST", CHECK_RUNS)) == 1
    assert github.requests_to("PATCH", CHECK_RUN) == []


async def test_the_search_looks_among_the_check_runs_of_the_commit_by_app(
    reporter: GitHubReporter, github: FakeGitHub
) -> None:
    await reporter.start_check(RUN, None)

    (listing,) = github.requests_to("GET", COMMIT_CHECK_RUNS)
    assert listing.params["ref"] == SHA
    assert listing.query["app_id"] == str(APP_ID)
    assert listing.query["filter"] == "all"


async def test_a_known_id_is_patched_in_progress_without_a_search_or_a_post(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    existing = repo.add_check_run(
        name="CICD Doctor: CI",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="in_progress",
        conclusion=None,
    )

    check_id = await reporter.start_check(make_run(run_attempt=2), existing["id"])

    assert check_id == existing["id"]
    assert existing["output"]["summary"] == "Analyzing failure… (attempt 2)"
    assert len(github.requests_to("PATCH", CHECK_RUN)) == 1
    assert github.requests_to("POST", CHECK_RUNS) == []
    assert github.requests_to("GET", COMMIT_CHECK_RUNS) == []


async def test_a_renamed_workflow_renames_the_check_run(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    existing = repo.add_check_run(
        name="CICD Doctor: Old name",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="in_progress",
        conclusion=None,
    )

    await reporter.start_check(make_run(workflow_name="New   name"), existing["id"])

    assert existing["name"] == "CICD Doctor: New name"


async def test_a_hostile_workflow_name_is_sanitised_in_the_check_name(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    run = make_run(workflow_name="deploy\n‮<b>x</b> " + "w" * 100)

    check_id = await reporter.start_check(run, None)

    name = repo.check_runs[check_id]["name"]
    assert name == check_name(run.workflow_name)
    assert "\n" not in name
    assert "‮" not in name
    assert len(name) <= 80


async def test_a_completed_check_run_goes_back_to_in_progress_when_github_allows_it(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    repo.allow_reopen = True
    existing = repo.add_check_run(
        name="CICD Doctor: CI",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="completed",
        conclusion="neutral",
    )

    await reporter.start_check(make_run(run_attempt=2), existing["id"])

    assert existing["status"] == "in_progress"
    assert existing["conclusion"] is None
    assert existing["output"]["summary"] == "Analyzing failure… (attempt 2)"


async def test_when_github_refuses_to_reopen_only_the_output_changes_and_it_stays_completed(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.allow_reopen = False
    existing = repo.add_check_run(
        name="CICD Doctor: CI",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="completed",
        conclusion="neutral",
    )

    check_id = await reporter.start_check(make_run(run_attempt=2), existing["id"])

    assert check_id == existing["id"]
    assert existing["status"] == "completed"
    assert existing["conclusion"] == "neutral"
    assert existing["output"]["summary"] == "Analyzing failure… (attempt 2)"
    first, second = github.requests_to("PATCH", CHECK_RUN)
    assert first.json["status"] == "in_progress"
    assert second.json == {"output": first.json["output"]}  # nothing but the output
    assert github.requests_to("POST", CHECK_RUNS) == []


async def test_a_lost_id_is_found_again_by_external_id(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    ours = repo.add_check_run(
        name="CICD Doctor: CI",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="in_progress",
        conclusion=None,
    )

    check_id = await reporter.start_check(RUN, 123456)  # an id GitHub does not know

    assert check_id == ours["id"]
    assert github.requests_to("POST", CHECK_RUNS) == []
    assert len(github.requests_to("GET", COMMIT_CHECK_RUNS)) == 1


async def test_without_an_id_an_existing_check_run_is_found_and_reused(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    ours = repo.add_check_run(
        name="CICD Doctor: CI",
        head_sha=SHA,
        external_id=external_id_for(RUN.run_id),
        status="completed",
        conclusion="neutral",
    )

    assert await reporter.start_check(RUN, None) == ours["id"]
    assert github.requests_to("POST", CHECK_RUNS) == []


async def test_a_lost_id_and_nothing_found_creates_a_new_check_run(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    check_id = await reporter.start_check(RUN, 123456)

    assert check_id in repo.check_runs
    assert len(github.requests_to("POST", CHECK_RUNS)) == 1


async def test_the_search_ignores_other_apps_other_runs_and_check_runs_with_our_name(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    same_name = "CICD Doctor: CI"
    repo.add_check_run(name=same_name, head_sha=SHA, external_id=external_id_for(1))  # other run
    repo.add_check_run(name=same_name, head_sha=SHA, external_id=None)  # a uuid, as Actions' own
    repo.add_check_run(  # our external_id, but another app wrote it
        name=same_name, head_sha=SHA, external_id=external_id_for(RUN.run_id), app_id=999
    )
    repo.add_check_run(  # ours, but of another commit
        name=same_name, head_sha="b" * 40, external_id=external_id_for(RUN.run_id)
    )

    check_id = await reporter.start_check(RUN, None)

    assert repo.check_runs[check_id]["external_id"] == external_id_for(RUN.run_id)
    assert repo.check_runs[check_id]["app"]["id"] == APP_ID
    assert len(github.requests_to("POST", CHECK_RUNS)) == 1


async def test_the_search_pages_through_a_commit_full_of_check_runs(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.page_cap = 5
    for _ in range(12):  # the lab has 30 check runs of one name on a commit
        repo.add_check_run(name="run", head_sha=SHA, external_id=None)
    ours = repo.add_check_run(
        name="old", head_sha=SHA, external_id=external_id_for(RUN.run_id), status="in_progress"
    )

    assert await reporter.start_check(RUN, None) == ours["id"]
    assert len(github.requests_to("GET", COMMIT_CHECK_RUNS)) == 3


async def test_a_failed_patch_is_not_hidden_by_a_search(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    existing = repo.add_check_run(head_sha=SHA, external_id=external_id_for(RUN.run_id))
    github.fail("PATCH", CHECK_RUN, 500, times=1)

    with pytest.raises(GitHubTransientError) as caught:
        await reporter.start_check(RUN, existing["id"])

    assert CANARY not in str(caught.value)
    assert github.requests_to("POST", CHECK_RUNS) == []


async def test_a_refused_permission_is_permanent(
    reporter: GitHubReporter, github: FakeGitHub, repo: FakeRepoWrites
) -> None:
    github.fail("POST", CHECK_RUNS, 403, message="Resource not accessible by integration")

    with pytest.raises(GitHubPermanentError) as caught:
        await reporter.start_check(RUN, None)

    assert caught.value.status_code == 403
    assert CANARY not in str(caught.value)


# --- the check run: complete -----------------------------------------------------------------


async def test_complete_sends_neutral_with_the_title_and_the_summary(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub, clock: FakeClock
) -> None:
    check_id = await reporter.start_check(RUN, None)

    await reporter.complete_check(RUN, check_id, OUTPUT)

    done = repo.check_runs[check_id]
    assert (done["status"], done["conclusion"]) == ("completed", "neutral")
    assert done["completed_at"] == clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    assert done["output"]["title"] == OUTPUT.title
    assert done["output"]["summary"] == OUTPUT.summary
    sent = github.requests_to("PATCH", CHECK_RUN)[-1].json
    assert set(sent) == {"status", "conclusion", "completed_at", "output"}
    assert set(sent["output"]) == {"title", "summary"}


async def test_complete_keeps_the_output_within_the_limits_of_the_checks_api(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    check_id = await reporter.start_check(RUN, None)

    await reporter.complete_check(
        RUN, check_id, CheckOutput(title="t" * 1000, summary="s" * (MAX_CHECK_SUMMARY + 5000))
    )

    output = repo.check_runs[check_id]["output"]
    assert len(output["title"]) == 255
    assert len(output["summary"]) == MAX_CHECK_SUMMARY


async def test_a_check_run_that_no_longer_exists_fails_to_complete(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    with pytest.raises(GitHubPermanentError) as caught:
        await reporter.complete_check(RUN, 424242, OUTPUT)

    assert caught.value.status_code == 404


# --- the sticky comment ----------------------------------------------------------------------


async def test_a_new_comment_is_posted_when_there_is_none(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    comment_id = await reporter.upsert_comment(TARGET, comment_body(), None)

    posted = repo.comments[comment_id]
    assert posted["body"] == comment_body()
    assert posted["user"]["login"] == BOT
    assert len(github.requests_to("POST", ISSUE_COMMENTS)) == 1
    assert github.requests_to("PATCH", ISSUE_COMMENT) == []


async def test_a_known_id_is_patched_without_a_search_or_a_post(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    existing = repo.add_bot_comment(PR, comment_body("old"))

    comment_id = await reporter.upsert_comment(TARGET, comment_body("new"), existing["id"])

    assert comment_id == existing["id"]
    assert existing["body"] == comment_body("new")
    assert github.requests_to("GET", ISSUE_COMMENTS) == []
    assert github.requests_to("POST", ISSUE_COMMENTS) == []


async def test_a_lost_id_is_found_again_by_the_marker_among_the_comments_of_the_bot(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.add_user_comment(PR, "looks good to me")
    ours = repo.add_bot_comment(PR, comment_body("old"))

    comment_id = await reporter.upsert_comment(TARGET, comment_body("new"), 123456)

    assert comment_id == ours["id"]
    assert ours["body"] == comment_body("new")
    assert github.requests_to("POST", ISSUE_COMMENTS) == []


async def test_without_an_id_the_comment_of_the_bot_is_found_and_updated(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    ours = repo.add_bot_comment(PR, comment_body("old"))

    assert await reporter.upsert_comment(TARGET, comment_body("new"), None) == ours["id"]
    assert github.requests_to("POST", ISSUE_COMMENTS) == []


async def test_a_lost_id_and_no_marker_posts_a_new_comment(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    comment_id = await reporter.upsert_comment(TARGET, comment_body(), 123456)

    assert repo.comments[comment_id]["user"]["login"] == BOT
    assert len(github.requests_to("POST", ISSUE_COMMENTS)) == 1


async def test_the_marker_copied_into_a_comment_of_another_user_is_ignored(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    """Security (D-70): a user can neither hijack the sticky comment nor block it."""
    theirs = repo.add_user_comment(PR, comment_body("mine now"))

    comment_id = await reporter.upsert_comment(TARGET, comment_body("diagnosis"), None)

    assert comment_id != theirs["id"]
    assert theirs["body"] == comment_body("mine now")  # untouched
    assert github.requests_to("PATCH", ISSUE_COMMENT) == []
    assert repo.comments[comment_id]["user"]["login"] == BOT


async def test_the_bot_comment_wins_over_an_older_copy_of_the_marker_by_a_user(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    repo.add_user_comment(PR, comment_body("copy"))  # older: a lower id
    ours = repo.add_bot_comment(PR, comment_body("ours"))

    assert await reporter.upsert_comment(TARGET, comment_body("new"), None) == ours["id"]


async def test_a_comment_of_the_bot_for_another_workflow_or_pr_is_ignored(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    other_workflow = repo.add_bot_comment(PR, comment_body("ci", comment_marker(PR, 8)))
    other_pr = repo.add_bot_comment(PR, comment_body("pr", comment_marker(99, WORKFLOW)))

    comment_id = await reporter.upsert_comment(TARGET, comment_body("diagnosis"), None)

    assert comment_id not in {other_workflow["id"], other_pr["id"]}
    assert other_workflow["body"] == comment_body("ci", comment_marker(PR, 8))
    assert github.requests_to("PATCH", ISSUE_COMMENT) == []


async def test_the_marker_must_be_the_whole_first_line(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    quoting = repo.add_bot_comment(PR, f"I quote:\n{MARKER}")
    longer = repo.add_bot_comment(PR, f"{MARKER} and more\ntext")

    comment_id = await reporter.upsert_comment(TARGET, comment_body(), None)

    assert comment_id not in {quoting["id"], longer["id"]}


async def test_a_comment_with_windows_line_endings_is_still_ours(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    ours = repo.add_bot_comment(PR, f"{MARKER}\r\n### CICD Doctor: CI\r\n")

    assert await reporter.upsert_comment(TARGET, comment_body(), None) == ours["id"]


async def test_the_comments_are_paginated(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.page_cap = 3
    for index in range(7):
        repo.add_user_comment(PR, f"comment {index}")
    ours = repo.add_bot_comment(PR, comment_body("old"))  # the 8th: on page 3

    assert await reporter.upsert_comment(TARGET, comment_body("new"), None) == ours["id"]
    assert len(github.requests_to("GET", ISSUE_COMMENTS)) == 3


async def test_the_search_stops_at_ten_pages_and_then_posts(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    repo.page_cap = 2
    for index in range(20):
        repo.add_user_comment(PR, f"comment {index}")
    beyond = repo.add_bot_comment(PR, comment_body("old"))  # the 21st: on page 11

    comment_id = await reporter.upsert_comment(TARGET, comment_body("new"), None)

    assert len(github.requests_to("GET", ISSUE_COMMENTS)) == 10
    assert comment_id != beyond["id"]
    assert len(github.requests_to("POST", ISSUE_COMMENTS)) == 1


async def test_a_known_id_of_a_comment_the_bot_did_not_write_is_never_edited(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    theirs = repo.add_user_comment(PR, "not ours")

    with pytest.raises(GitHubPermanentError) as caught:
        await reporter.upsert_comment(TARGET, comment_body(), theirs["id"])

    assert caught.value.status_code == 403
    assert theirs["body"] == "not ours"


async def test_a_body_without_the_marker_of_its_target_is_refused_before_any_request(
    reporter: GitHubReporter, github: FakeGitHub
) -> None:
    for body in ("no marker", comment_body(marker=comment_marker(PR, 8)), f"\n{MARKER}"):
        with pytest.raises(PermanentError):
            await reporter.upsert_comment(TARGET, body, None)

    assert github.requests == []


async def test_the_comment_the_renderer_writes_is_found_again(
    reporter: GitHubReporter, repo: FakeRepoWrites
) -> None:
    diagnosis = Diagnosis(
        category=Category.DEPENDENCY,
        probable_cause="The module `requests` is not installed.",
        evidence=[Evidence(line=10, text="ModuleNotFoundError: No module named 'requests'")],
        suggested_fix="Declare requests in the dependencies.",
        confidence=0.9,
        source="rules",
        rule_ids=["PY-MODNF"],
    )
    body = render_comment(DiagnosisReport(run=RUN, items=[(make_failure(run=RUN), diagnosis)]))
    assert body.startswith(MARKER + "\n")
    assert len(body) <= MAX_COMMENT_CHARS

    first = await reporter.upsert_comment(TARGET, body, None)
    second = await reporter.upsert_comment(TARGET, body, None)

    assert first == second
    assert len(repo.comments) == 1


# --- the token --------------------------------------------------------------------------------


async def test_every_call_uses_the_token_of_the_diagnosis_permissions_for_one_repository(
    reporter: GitHubReporter, repo: FakeRepoWrites, github: FakeGitHub
) -> None:
    check_id = await reporter.start_check(RUN, None)
    await reporter.complete_check(RUN, check_id, OUTPUT)
    await reporter.upsert_comment(TARGET, comment_body(), None)
    await reporter.upsert_comment(TARGET, comment_body("again"), None)

    assert github.issued, "no installation token was requested"
    for token in github.issued:
        assert token.installation_id == INSTALLATION_ID
        assert token.repo_ids == (REPO_ID,)
        assert set(token.permissions.items()) == set(DIAGNOSIS_PERMISSIONS)
    api = [r for r in github.requests if r.template and r.template.startswith("/repos/")]
    assert api
    assert all(any(r.has_bearer(t.token) for t in github.issued) for r in api)
