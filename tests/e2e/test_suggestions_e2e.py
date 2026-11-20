"""Offline end to end of the suggested changes (P3 e, D-97): signed webhook or ``explain`` job ->
worker -> the real use case and GitHub adapters over ``FakeGitHub``, whose diff routes know which
lines a review comment may anchor to. A model that suggests inside the diff gets one review
comment with a ``suggestion`` block; one that points elsewhere, or a GitHub that refuses, leaves
the text only. No network and no Postgres.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.render import SUGGESTION_TEXT
from tests.diagnose_world import PR_NUMBER, DiagnoseWorld, diagnose_world, workflow_run_payload
from tests.diffs import patch_of
from tests.e2e.test_diagnose_e2e import CHECK, COMMENTS, deliver, run_job
from tests.e2e.test_explain_upgrade_e2e import FORK, HEAD_SHA, enqueue

PULL_COMMENTS = "/repos/{owner}/{repo}/pulls/{pull_number}/comments"
PULL_COMMENT = "/repos/{owner}/{repo}/pulls/comments/{comment_id}"
PULL_FILES = "/repos/{owner}/{repo}/pulls/{number}/files"
PATH = "src/app/cli.py"  # named by the traceback of the default log
PATCH = patch_of((1, 1, [f" line {n}" for n in range(1, 11)] + ["+ added 11"]))  # lines 1-11
WEBHOOK_SHA = str(workflow_run_payload()["workflow_run"]["head_sha"])


def suggestion(start: int = 3, end: int = 4, text: str = "x = 1") -> dict[str, Any]:
    return {"path": PATH, "start_line": start, "end_line": end, "replacement": text}


def files() -> list[dict[str, Any]]:
    return [
        {"sha": "0" * 40, "filename": PATH, "status": "modified", "patch": PATCH},
        {"sha": "1" * 40, "filename": "uv.lock", "status": "modified"},  # too big: no patch
    ]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))


async def build(clock: FakeClock, script: list[Any]) -> AsyncIterator[DiagnoseWorld]:
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
        llm_script=script,
    ) as built:
        yield built


@pytest.fixture
async def inside(clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async for built in build(clock, [valid(suggestion=suggestion())]):
        yield built


@pytest.fixture
async def outside(clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async for built in build(clock, [valid(suggestion=suggestion(start=40, end=41))]):
        yield built


@pytest.fixture
async def fork_world(clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async for built in build(clock, []):
        yield built


def pull_request(world: DiagnoseWorld, *, sha: str = WEBHOOK_SHA) -> None:
    world.writes.add_pull(PR_NUMBER, head_repo="acme/widgets", head_ref="feature/x", head_sha=sha)
    world.writes.add_pull_files(PR_NUMBER, files())


async def test_a_suggestion_inside_the_diff_becomes_one_review_comment(
    inside: DiagnoseWorld,
) -> None:
    world = inside
    world.add_run()
    pull_request(world)

    assert deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e1") == 202
    assert await run_job(world)

    [files_request] = world.requests("GET", PULL_FILES)
    assert files_request.params["number"] == str(PR_NUMBER)
    [posted] = world.requests("POST", PULL_COMMENTS)
    body = posted.json
    assert (body["path"], body["start_line"], body["line"], body["side"]) == (PATH, 3, 4, "RIGHT")
    assert body["commit_id"] == WEBHOOK_SHA
    assert "```suggestion\nx = 1\n```" in body["body"]
    assert body["body"].startswith("<!-- cicd-doctor:v1 suggestion pr=12 failure=")
    [check] = world.requests("PATCH", CHECK)
    assert SUGGESTION_TEXT["posted"] in check.json["output"]["summary"]
    [sticky] = world.requests("POST", COMMENTS)
    assert SUGGESTION_TEXT["posted"] in sticky.json["body"]
    assert len(world.llm.calls) == 1
    user = world.llm.messages[0].user
    assert "11+  added 11" in user
    assert "uv.lock" not in user


async def test_the_prompt_never_names_a_file_the_log_does_not_name(inside: DiagnoseWorld) -> None:
    world = inside
    world.add_run()
    pull_request(world)
    world.writes.add_pull_files(
        PR_NUMBER, [*files(), {"filename": "docs/other.md", "status": "modified", "patch": PATCH}]
    )

    deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e2")
    await run_job(world)

    assert "docs/other.md" not in world.llm.messages[0].user


async def test_a_suggestion_outside_the_hunks_leaves_the_text_only(outside: DiagnoseWorld) -> None:
    world = outside
    world.add_run()
    pull_request(world)

    deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e3")
    assert await run_job(world)

    assert world.requests("POST", PULL_COMMENTS) == []
    [check] = world.requests("PATCH", CHECK)
    assert SUGGESTION_TEXT["posted"] not in check.json["output"]["summary"]
    assert "Suggested fix" in check.json["output"]["summary"]


async def test_a_pull_request_that_moved_gets_no_diff_and_no_suggestion(
    inside: DiagnoseWorld,
) -> None:
    world = inside
    world.add_run()
    pull_request(world, sha="e" * 40)  # the head is not the commit of the run

    deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e4")
    assert await run_job(world)

    assert world.requests("GET", PULL_FILES) == []
    assert world.requests("POST", PULL_COMMENTS) == []
    assert "untrusted_diff" not in world.llm.messages[0].user


async def test_a_422_from_github_is_text_only_and_the_job_still_finishes(
    inside: DiagnoseWorld,
) -> None:
    world = inside
    world.add_run()
    pull_request(world)
    world.writes.review_status = 422

    deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e5")
    assert await run_job(world)

    [check] = world.requests("PATCH", CHECK)
    assert SUGGESTION_TEXT["outside_diff"] in check.json["output"]["summary"]
    assert world.writes.review_comments == {}
    assert len(world.requests("POST", COMMENTS)) == 1


async def test_a_403_names_the_missing_permission_in_the_report(inside: DiagnoseWorld) -> None:
    world = inside
    world.add_run()
    pull_request(world)
    world.writes.review_status = 403

    deliver(world, delivery="b1c2d3e4-0000-4000-8000-0000000000e6")
    assert await run_job(world)

    [check] = world.requests("PATCH", CHECK)
    assert SUGGESTION_TEXT["permission"] in check.json["output"]["summary"]


async def test_a_fork_command_posts_the_suggestion_and_a_second_one_edits_it(
    fork_world: DiagnoseWorld,
) -> None:
    world = fork_world
    world.add_run()
    world.writes.add_pull(PR_NUMBER, head_repo=FORK, head_ref="patch-1", head_sha=HEAD_SHA)
    world.writes.add_pull_files(PR_NUMBER, files())
    await enqueue(world, explain=False)
    assert await run_job(world)  # the automatic job: a fork gets the rules only
    assert world.requests("GET", PULL_FILES) == []
    world.llm.add(valid(suggestion=suggestion()), valid(suggestion=suggestion()))

    await enqueue(world, explain=True)
    assert await run_job(world)

    [posted] = world.requests("POST", PULL_COMMENTS)
    assert posted.json["commit_id"] == HEAD_SHA
    [check] = world.requests("PATCH", CHECK)[-1:]
    assert SUGGESTION_TEXT["posted"] in check.json["output"]["summary"]
    # a second command on the same run: nothing new is created, the same comment is edited
    assert await world.queue.enqueue(_second_command(), None)
    assert await run_job(world)
    assert len(world.requests("POST", PULL_COMMENTS)) == 1
    assert len(world.writes.review_comments) == 1
    assert len(world.requests("PATCH", PULL_COMMENT)) == 1
    assert len(world.llm.calls) == 1  # the diagnosis was already upgraded: no second model call


def _second_command() -> Any:
    from tests.e2e.test_explain_upgrade_e2e import fork_explain_job

    job = fork_explain_job()
    return job.model_copy(update={"dedupe_key": "explain:100:5000:1"})
