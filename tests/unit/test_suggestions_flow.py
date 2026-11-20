"""Suggested changes in the diagnosis use case (P3 e, D-97), on in-memory fakes.

The model is shown the hunks of the files the log names; a suggestion that points outside them is
dropped; a valid one becomes a review comment under the sticky comment's lock, once, and the check
run and the comment say what happened to it.
"""

import json
from typing import Any

import pytest

from cicd_doctor.adapters.github.http import GitHubPermanentError
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import (
    DiffFile,
    LoadedConfig,
    PullRequestInfo,
    RepoConfig,
    RunRef,
)
from cicd_doctor.core.render import SUGGESTION_TEXT
from cicd_doctor.core.suggestions import suggestion_marker
from tests.diffs import patch_of, pull_diff
from tests.factories import make_run_ref
from tests.unit.test_diagnose_failed_run import World, log_text, workflow_job

PR = 12
HEAD = "a" * 40
PATH = "src/app/cli.py"  # named by the traceback of the default log
HUNK = patch_of((1, 1, [f" line {n}" for n in range(1, 11)] + ["+ added 11"]))  # new lines 1-11


def suggestion(
    path: str = PATH, start: int = 3, end: int = 4, text: str = "x = 1"
) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end, "replacement": text}


def pull(head_sha: str = HEAD, **changes: Any) -> PullRequestInfo:
    fields: dict[str, Any] = {
        "number": PR,
        "state": "open",
        "draft": False,
        "merged": False,
        "head_sha": head_sha,
        "head_branch": "feature/x",
        "head_repo_id": 100,
        "head_repo_full_name": "acme/widgets",
        "base_repo_id": 100,
    }
    return PullRequestInfo(**(fields | changes))


def with_diff(world: World, *files: DiffFile, head_sha: str = HEAD) -> World:
    world.pulls.add(world.run.repo_id, pull(head_sha))
    world.pulls.add_diff(world.run.repo_id, PR, pull_diff(*files))
    return world


def cli_file(patch: str = HUNK) -> DiffFile:
    return DiffFile(filename=PATH, status="modified", patch=patch)


def prompt_of(world: World, call: int = 0) -> str:
    return world.llm.messages[call].user


def posted(world: World) -> list[Any]:
    return list(world.reporter.review_comments.values())


async def summary(world: World) -> str:
    [check] = world.reporter.checks.values()
    assert check.output is not None
    return check.output.summary


# --- the model sees the diff ---------------------------------------------------------------


async def test_the_prompt_has_the_hunks_of_the_files_the_log_names() -> None:
    other = DiffFile(filename="docs/unrelated.md", status="modified", patch=HUNK)
    world = with_diff(World([valid()]), cli_file(), other)

    await world.run_once()

    user = prompt_of(world)
    assert '<untrusted_diff nonce="' in user
    assert "file: src/app/cli.py (modified)" in user
    assert "@@ lines 1-11 @@" in user
    assert "11+  added 11" in user
    assert "docs/unrelated.md" not in user


async def test_no_pull_request_diff_means_no_block_and_no_suggestion() -> None:
    world = World([valid(suggestion=suggestion())])  # the fake knows no pull request

    await world.run_once()

    assert "untrusted_diff" not in prompt_of(world)
    assert (await world.record()).items[0].published.suggestion is None
    assert posted(world) == []


@pytest.mark.parametrize(
    "state",
    [{"head_sha": "b" * 40}, {"state": "closed"}],
    ids=["the head moved", "the pull request is closed"],
)
async def test_the_diff_is_not_used_when_the_pull_request_is_not_the_one_of_the_run(
    state: dict[str, Any],
) -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    world.pulls.add(world.run.repo_id, pull(**state))

    await world.run_once()

    assert "untrusted_diff" not in prompt_of(world)
    assert world.pulls.diffs == []  # the files are not even listed
    assert posted(world) == []


@pytest.mark.parametrize(
    "error", [TransientError("5xx"), GitHubPermanentError("403", status_code=403)]
)
async def test_an_error_reading_the_diff_costs_only_the_suggestion(error: Exception) -> None:
    world = World([valid(suggestion=suggestion())])
    world.pulls.add(world.run.repo_id, pull())
    original = world.pulls.diff

    async def failing(repo: Any, number: int) -> Any:
        raise error

    world.pulls.diff = failing  # type: ignore[method-assign]
    await world.run_once()
    world.pulls.diff = original  # type: ignore[method-assign]

    assert "untrusted_diff" not in prompt_of(world)
    [check] = world.reporter.checks.values()
    assert check.status == "completed"  # the diagnosis was published all the same
    assert posted(world) == []


async def test_a_truncated_diff_is_not_shown() -> None:
    world = World([valid(suggestion=suggestion())])
    world.pulls.add(world.run.repo_id, pull())
    world.pulls.add_diff(world.run.repo_id, PR, pull_diff(cli_file(), truncated=True))

    await world.run_once()

    assert "untrusted_diff" not in prompt_of(world)
    assert posted(world) == []


async def test_the_diff_is_not_read_when_the_model_cannot_be_called() -> None:
    world = with_diff(World(with_llm=False), cli_file())

    await world.run_once()

    assert world.pulls.gets == []
    assert world.pulls.diffs == []


async def test_the_diff_is_not_read_when_the_repository_turned_comments_off() -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    world.repo_config.set(LoadedConfig(config=RepoConfig(comment=False), state="file"))

    await world.run_once()

    assert world.pulls.diffs == []
    assert "untrusted_diff" not in prompt_of(world)
    assert posted(world) == []
    assert world.reporter.comments == {}


# --- a suggestion inside the diff -----------------------------------------------------------


async def test_a_suggestion_inside_a_hunk_becomes_one_review_comment() -> None:
    world = with_diff(World([valid(suggestion=suggestion(text="x = 1\ny = 2"))]), cli_file())

    await world.run_once()

    [comment] = posted(world)
    assert (comment.path, comment.start_line, comment.end_line) == (PATH, 3, 4)
    assert comment.commit_id == HEAD
    [item] = (await world.record()).items
    assert comment.body.split("\n")[0] == suggestion_marker(PR, item.failure.id)
    assert "```suggestion\nx = 1\ny = 2\n```" in comment.body
    assert SUGGESTION_TEXT["posted"] in await summary(world)
    [sticky] = world.reporter.comments.values()
    assert SUGGESTION_TEXT["posted"] in sticky.body


async def test_a_second_run_edits_the_review_comment_instead_of_adding_one() -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    await world.run_once()

    await world.run_once()  # a retry of the job, or a second command: nothing is analysed again

    [comment] = posted(world)
    assert comment.edits == 1
    assert len(world.llm.calls) == 1


async def test_a_suggestion_outside_every_hunk_is_dropped_and_the_text_stays() -> None:
    world = with_diff(World([valid(suggestion=suggestion(start=20, end=21))]), cli_file())

    await world.run_once()

    [item] = (await world.record()).items
    assert item.published.suggestion is None
    assert item.published.suggested_fix  # the text of the fix is still there
    assert posted(world) == []
    assert "suggestion" not in (await summary(world)).lower().replace("suggested fix", "")


@pytest.mark.parametrize(
    "bad",
    [
        suggestion(path="src/other.py"),
        suggestion(path="../" + PATH),
        suggestion(path="/" + PATH),
        suggestion(start=11, end=12),  # one line past the hunk
        suggestion(text=""),
    ],
    ids=["other file", "dot-dot", "absolute", "past the hunk", "empty"],
)
async def test_a_misplaced_suggestion_never_reaches_github(bad: dict[str, Any]) -> None:
    world = with_diff(World([valid(suggestion=bad)]), cli_file())

    await world.run_once()

    assert posted(world) == []
    assert world.reporter.methods().count("post_suggestion") == 0


async def test_a_range_over_two_hunks_is_dropped() -> None:
    two = patch_of((1, 1, [" a", "+b"]), (30, 31, [" c", "+d"]))
    world = with_diff(World([valid(suggestion=suggestion(start=2, end=31))]), cli_file(two))

    await world.run_once()

    assert posted(world) == []


async def test_a_secret_in_the_replacement_drops_the_suggestion() -> None:
    from tests.redaction_corpus import planted_secrets

    planted = planted_secrets()[0]
    text = f"token = '{planted.secret}'"
    world = with_diff(World([valid(suggestion=suggestion(text=text))]), cli_file())

    await world.run_once()

    assert posted(world) == []
    assert planted.secret not in json.dumps([c.body for c in world.reporter.comments.values()])


async def test_a_suggestion_of_a_removed_or_patchless_file_cannot_exist() -> None:
    gone = DiffFile(filename=PATH, status="removed", patch=HUNK)
    world = with_diff(World([valid(suggestion=suggestion())]), gone)

    await world.run_once()

    assert "untrusted_diff" not in prompt_of(world)
    assert posted(world) == []


# --- what happens when GitHub does not take it ----------------------------------------------


async def test_a_422_leaves_the_text_and_says_why() -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    world.reporter.reject_suggestions = True

    await world.run_once()

    assert posted(world) == []
    assert SUGGESTION_TEXT["outside_diff"] in await summary(world)
    [check] = world.reporter.checks.values()
    assert check.status == "completed"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (GitHubPermanentError("no", status_code=403), "permission"),
        (GitHubPermanentError("gone", status_code=404), "unavailable"),
        (TransientError("502"), "unavailable"),
    ],
)
async def test_an_error_posting_never_fails_the_diagnosis(error: Exception, key: str) -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    world.reporter.fail_next("post_suggestion", error)

    await world.run_once()

    assert SUGGESTION_TEXT[key] in await summary(world)
    [check] = world.reporter.checks.values()
    assert check.status == "completed"
    assert len(world.reporter.comments) == 1  # the sticky comment was written


async def test_the_pull_request_moving_before_posting_posts_nothing() -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    calls = {"n": 0}
    real = world.pulls.get

    async def moving(repo: Any, number: int, *, for_explain: bool = True) -> Any:
        calls["n"] += 1
        found = await real(repo, number, for_explain=for_explain)
        # the second read (right before posting) finds a new head
        assert found is not None
        return found.model_copy(update={"head_sha": "c" * 40}) if calls["n"] > 1 else found

    world.pulls.get = moving  # type: ignore[method-assign]
    await world.run_once()

    assert posted(world) == []
    assert SUGGESTION_TEXT["head_moved"] in await summary(world)


async def test_a_closed_pull_request_posts_nothing() -> None:
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    calls = {"n": 0}
    real = world.pulls.get

    async def closing(repo: Any, number: int, *, for_explain: bool = True) -> Any:
        calls["n"] += 1
        found = await real(repo, number, for_explain=for_explain)
        assert found is not None
        return found.model_copy(update={"state": "closed"}) if calls["n"] > 1 else found

    world.pulls.get = closing  # type: ignore[method-assign]
    await world.run_once()

    assert posted(world) == []
    assert SUGGESTION_TEXT["closed"] in await summary(world)


# --- how many, and for whom -----------------------------------------------------------------


async def test_at_most_the_configured_number_of_suggestions_per_attempt() -> None:
    from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig

    world = with_diff(
        World([valid(suggestion=suggestion())] * 3, config=DiagnoseConfig(max_suggestions=2)),
        cli_file(),
    )
    jobs = [workflow_job(job_id) for job_id in (11, 21, 31)]
    world.logs.add_jobs(world.run, jobs)
    for job_id in (21, 31):
        world.logs.add_log(job_id, log_text())

    await world.run_once()

    assert len(posted(world)) == 2
    assert SUGGESTION_TEXT["limit"] in await summary(world)


async def test_zero_turns_the_suggestions_off() -> None:
    from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig

    world = with_diff(
        World([valid(suggestion=suggestion())], config=DiagnoseConfig(max_suggestions=0)),
        cli_file(),
    )

    await world.run_once()

    assert posted(world) == []


async def test_a_cached_diagnosis_never_carries_a_suggestion() -> None:
    from cicd_doctor.core.diagnosis.cache import reuse_cached
    from cicd_doctor.core.models import Suggestion
    from tests.factories import make_diagnosis, make_failure

    diagnosis = make_diagnosis(
        suggestion=Suggestion(path=PATH, start_line=1, end_line=1, replacement="x")
    )
    reused = reuse_cached(diagnosis, make_failure().excerpt)

    assert reused is None or reused.suggestion is None


async def test_a_fork_command_posts_the_suggestion_on_the_pull_request() -> None:
    fork: RunRef = make_run_ref(
        is_fork=True,
        pr_number=None,
        head_repo_full_name="drive-by/widgets",
        head_branch="patch-1",
    )
    world = World(script=[], run=fork)
    world.pulls.set(5000, PR)
    await world.run_once()  # the automatic job: rules only, a fork never calls the model
    assert posted(world) == []
    with_diff(world, cli_file())
    world.llm.add(valid(suggestion=suggestion()))

    await world.run_once(trigger="explain", pr_number=PR, command_comment_id=99)

    [comment] = posted(world)
    assert comment.commit_id == fork.head_sha
    assert SUGGESTION_TEXT["posted"] in await summary(world)


async def test_an_old_stored_suggestion_is_not_posted() -> None:
    """A diagnosis stored by an older prompt was never checked against a diff."""
    world = with_diff(World([valid(suggestion=suggestion())]), cli_file())
    await world.run_once()
    [comment] = posted(world)
    world.reporter.review_comments.clear()
    record = await world.record()
    stale = record.items[0].published.model_copy(update={"prompt_version": "diag-v2"})
    from cicd_doctor.core.usecases.suggest import SuggestionPublisher

    publisher = SuggestionPublisher(pulls=world.pulls, reporter=world.reporter, store=world.store)
    item = record.items[0].model_copy(update={"published": stale})
    outcomes = await publisher.publish(
        world.run,
        __import__("cicd_doctor.core.models", fromlist=["RepoRef"]).RepoRef(
            installation_id=1, repo_id=100, full_name="acme/widgets", default_branch="main"
        ),
        PR,
        record.model_copy(update={"items": [item]}),
    )

    assert outcomes == {}
    assert posted(world) == []
    assert comment.path == PATH
