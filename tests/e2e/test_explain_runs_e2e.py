"""Offline end to end of the whole command (P3 d2, D-96): a signed ``issue_comment`` webhook becomes
an ``explain`` job; the worker reads the pull request and the runs of its head, enqueues one child
``diagnose_run`` per failed run (three at most) and each child upgrades the rules diagnosis that a
fork run got into the LLM one, editing the same check run and the same sticky comment. The real
API, worker, use cases and GitHub adapters over ``FakeGitHub``; no network and no Postgres."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.webhooks import route_event
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.core.models import DiagnoseRunPayload, JobKind, JobStatus
from cicd_doctor.core.render import FALLBACK_TEXT
from tests.diagnose_world import PR_NUMBER, DiagnoseWorld, diagnose_world, workflow_run_payload
from tests.e2e.test_diagnose_e2e import CHECK, CHECKS, COMMENT, COMMENTS, JOBS, LOG, run_job
from tests.e2e.test_explain_e2e import COMMAND
from tests.webhooks import SECRET, encode, headers, load

WEBHOOK = "/webhooks/github"
RUNS = "/repos/{owner}/{repo}/actions/runs"
PULL = "/repos/{owner}/{repo}/pulls/{number}"
HEAD_SHA = "d" * 40
FORK = "drive-by/widgets"
FORK_ID = 777
REPO_ID = 100  # the ids of the webhook fixtures


@pytest.fixture
async def world() -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-20T12:00:00+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
        llm_script=[valid()] * 9,  # the cap of the plan: 3 runs x 3 jobs
    ) as built:
        yield built


def fork_run_payload(run_id: int, workflow_id: int) -> dict[str, Any]:
    """The ``workflow_run`` of a fork pull request: its ``pull_requests`` is empty on GitHub."""
    payload = workflow_run_payload(run_id, 1, head_sha=HEAD_SHA)
    run = payload["workflow_run"]
    run["pull_requests"] = []
    run["head_branch"] = "patch-1"
    run["workflow_id"] = workflow_id
    run["name"] = f"wf{workflow_id}"
    run["head_repository"] = {**run["head_repository"], "id": FORK_ID, "full_name": FORK}
    return payload


def fork_runs(world: DiagnoseWorld, ids: dict[int, int]) -> None:
    """The runs GitHub lists for the head: ``{run id: workflow id}`` (all failed, all forks)."""
    for run_id, workflow_id in ids.items():
        world.writes.add_run(
            run_id,
            head_sha=HEAD_SHA,
            workflow_id=workflow_id,
            name=f"wf{workflow_id}",
            head_repo=FORK,
            head_repo_id=FORK_ID,
            head_branch="patch-1",
            repo="acme/widgets",
        )


async def diagnose_automatically(world: DiagnoseWorld, run_id: int, workflow_id: int) -> None:
    """The webhook of the fork run, processed: rules only, the check run and the comment exist."""
    world.add_run(run_id)
    job = route_event(
        "workflow_run",
        fork_run_payload(run_id, workflow_id),
        received_at=world.clock.now(),
        max_attempts=5,
    )
    assert job is not None
    assert await world.queue.enqueue(job, None)
    assert await run_job(world)


def command_payload(world: DiagnoseWorld, comment_id: int, **changes: Any) -> dict[str, Any]:
    """The ``issue_comment`` webhook of a ``/cicd-doctor explain`` comment; the comment exists."""
    world.writes.comments[comment_id] = {
        "id": comment_id,
        "body": COMMAND,
        "user": {"login": "acme-maintainer", "type": "User"},
        "_repo": "acme/widgets",
        "_pr": PR_NUMBER,
    }
    payload = load("issue_comment_created")
    payload["comment"]["id"] = comment_id
    payload["comment"]["body"] = COMMAND
    payload["comment"].update(changes)
    return payload


async def enqueue_command(world: DiagnoseWorld, comment_id: int) -> None:
    """The job the webhook creates, without the HTTP hop (for the tests whose queue lives in
    another event loop than the test client's: Postgres)."""
    job = route_event(
        "issue_comment",
        command_payload(world, comment_id),
        received_at=world.clock.now(),
        max_attempts=5,
    )
    assert job is not None
    assert await world.queue.enqueue(job, None)


def command(world: DiagnoseWorld, comment_id: int, **changes: Any) -> int:
    """POST the signed comment to the real API; return its HTTP status."""
    deps = ApiDependencies(
        queue=world.queue, webhook_secrets=(SECRET,), ready=_ready, clock=world.clock
    )
    raw = encode(command_payload(world, comment_id, **changes))
    with TestClient(create_app(deps)) as client:
        return client.post(WEBHOOK, content=raw, headers=headers("issue_comment", raw)).status_code


async def _ready() -> bool:
    return True


async def drain(world: DiagnoseWorld) -> int:
    """Run every ready job; returns how many ran."""
    count = 0
    while await run_job(world):
        count += 1
    return count


def add_fork_pull(world: DiagnoseWorld, **changes: Any) -> None:
    world.writes.add_pull(
        PR_NUMBER,
        head_repo=FORK,
        head_ref="patch-1",
        head_sha=HEAD_SHA,
        head_repo_id=FORK_ID,
        base_repo_id=REPO_ID,
        **changes,
    )


async def test_a_command_upgrades_the_three_most_recent_failed_runs_of_a_fork_pull_request(
    world: DiagnoseWorld,
) -> None:
    add_fork_pull(world)
    fork_runs(world, {5000: 1, 5001: 2, 5002: 3, 5003: 4})
    world.writes.add_run(  # an older run of workflow 4 and a fork run that waits for approval
        4999, head_sha=HEAD_SHA, workflow_id=4, name="wf4", head_repo=FORK, head_repo_id=FORK_ID
    )
    world.writes.add_run(
        5004,
        head_sha=HEAD_SHA,
        workflow_id=5,
        name="wf5",
        conclusion="action_required",
        head_repo=FORK,
        head_repo_id=FORK_ID,
    )
    for run_id, workflow_id in ((5001, 2), (5002, 3), (5003, 4)):
        await diagnose_automatically(world, run_id, workflow_id)
    assert world.llm.calls == []  # a fork run never gets the model on its own
    checks_before = len(world.writes.check_runs)
    posts = (len(world.requests("POST", CHECKS)), len(world.requests("POST", COMMENTS)))
    downloads = len(world.requests("GET", LOG))
    world.add_run(5000)  # the oldest of the four is not diagnosed yet, and is not selected

    assert command(world, 777) == 202
    comments_before = len(world.writes.comments)  # the command comment is one of them
    assert await drain(world) == 4  # the command and its three children

    assert world.writes.reactions == [(777, "eyes")]  # one reaction, no second one at the end
    [listing] = world.requests("GET", RUNS)
    assert listing.query == {"head_sha": HEAD_SHA, "per_page": "100"}
    assert len(world.requests("GET", PULL)) == 1
    assert len(world.llm.calls) == 3  # three runs, one failed job each (9 would be the cap)
    assert len(world.requests("POST", CHECKS)) == posts[0]  # edited in place, never created again
    assert len(world.requests("POST", COMMENTS)) == posts[1]
    assert len(world.writes.check_runs) == checks_before
    assert len(world.writes.comments) == comments_before
    assert len(world.requests("GET", LOG)) == downloads  # the stored excerpt was used
    assert len(world.requests("GET", JOBS)) == 3
    for edit in world.requests("PATCH", COMMENT):
        assert "LLM " in edit.json["body"]
        assert FALLBACK_TEXT["fork_policy"] not in edit.json["body"]
    summaries = [r.json["output"]["summary"] for r in world.requests("PATCH", CHECK)]
    assert len(summaries) == 6  # three automatic completions, then the three upgrades
    assert all(FALLBACK_TEXT["fork_policy"] in text for text in summaries[:3])
    assert all(FALLBACK_TEXT["fork_policy"] not in text for text in summaries[3:])
    children = [await world.queue.get(i) for i in range(5, 8)]
    assert [c.dedupe_key for c in children if c] == [
        "explain:777:5003:1",
        "explain:777:5002:1",
        "explain:777:5001:1",
    ]
    for child in children:
        assert child is not None
        assert child.status is JobStatus.DONE
        parsed = DiagnoseRunPayload.model_validate(child.payload)
        assert (parsed.trigger, parsed.pr_number, parsed.command_comment_id) == (
            "explain",
            PR_NUMBER,
            777,
        )
        assert COMMAND not in str(child.payload)
    command_job = await world.queue.get(4)
    assert command_job is not None
    assert command_job.kind is JobKind.EXPLAIN
    assert command_job.status is JobStatus.DONE


async def test_a_second_command_within_five_minutes_creates_nothing(
    world: DiagnoseWorld,
) -> None:
    add_fork_pull(world)
    fork_runs(world, {5001: 2})
    await diagnose_automatically(world, 5001, 2)
    assert command(world, 777) == 202
    await drain(world)
    llm_calls = len(world.llm.calls)
    patches = len(world.requests("PATCH", CHECK))
    world.clock.advance(timedelta(minutes=4, seconds=59))

    assert command(world, 778) == 202
    assert await drain(world) == 1  # the job runs and ends in silence

    assert world.writes.reactions == [(777, "eyes")]
    assert len(world.llm.calls) == llm_calls
    assert len(world.requests("PATCH", CHECK)) == patches
    assert len(world.requests("GET", RUNS)) == 1


async def test_a_command_after_the_window_explains_again(world: DiagnoseWorld) -> None:
    add_fork_pull(world)
    fork_runs(world, {5001: 2})
    await diagnose_automatically(world, 5001, 2)
    command(world, 777)
    await drain(world)
    world.clock.advance(timedelta(minutes=5, seconds=1))

    command(world, 778)
    await drain(world)

    assert world.writes.reactions == [(777, "eyes"), (778, "eyes")]
    child = await world.queue.get(5)  # automatic, explain, child, explain, child
    assert child is not None
    assert child.dedupe_key == "explain:778:5001:1"


async def test_a_pull_request_with_no_failed_run_gets_a_confused_reaction(
    world: DiagnoseWorld,
) -> None:
    add_fork_pull(world)
    world.writes.add_run(
        5001,
        head_sha=HEAD_SHA,
        conclusion="action_required",  # the spike: a fork run waits for approval, no failure
        head_repo=FORK,
        head_repo_id=FORK_ID,
    )

    command(world, 777)
    assert await drain(world) == 1

    assert world.writes.reactions == [(777, "eyes"), (777, "confused")]
    assert world.llm.calls == []
    assert world.writes.check_runs == {}


async def test_the_runs_of_another_head_are_not_explained(world: DiagnoseWorld) -> None:
    add_fork_pull(world)  # the head moved: the old run is of the previous commit
    world.writes.add_run(
        5001, head_sha="e" * 40, head_repo=FORK, head_repo_id=FORK_ID, head_branch="patch-1"
    )

    command(world, 777)
    await drain(world)

    assert world.writes.reactions == [(777, "eyes"), (777, "confused")]


async def test_a_merged_pull_request_is_only_acknowledged(world: DiagnoseWorld) -> None:
    add_fork_pull(world, state="closed", merged=True)
    fork_runs(world, {5001: 2})

    command(world, 777)
    assert await drain(world) == 1

    assert world.writes.reactions == [(777, "eyes")]
    assert world.requests("GET", RUNS) == []
    assert world.llm.calls == []
