"""Offline end to end of the upgrade (P3 d1, D-94, D-95): a fork run is diagnosed with rules only,
then an ``explain`` ``diagnose_run`` job edits the SAME check run and the SAME sticky comment with
the LLM diagnosis. The real worker, use case and GitHub adapters over ``FakeGitHub``; no network
and no Postgres. The job of the command itself arrives in d2: here it is built by hand."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.webhooks import route_event
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.models import JobKind, JobStatus, NewJob
from cicd_doctor.core.render import FALLBACK_TEXT
from tests.diagnose_world import PR_NUMBER, DiagnoseWorld, diagnose_world, workflow_run_payload
from tests.e2e.test_diagnose_e2e import CHECK, CHECKS, COMMENT, COMMENTS, JOBS, LOG, run_job

HEAD_SHA = "d" * 40
FORK = "drive-by/widgets"


@pytest.fixture
async def world() -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
        llm_script=[],
    ) as built:
        yield built


def fork_payload(run_id: int = 5000, attempt: int = 1) -> dict[str, Any]:
    """The ``workflow_run`` of a pull request from a fork: another head repository, and the
    ``pull_requests`` array that GitHub leaves empty for it."""
    payload = workflow_run_payload(run_id, attempt, head_sha=HEAD_SHA)
    run = payload["workflow_run"]
    run["pull_requests"] = []
    run["head_branch"] = "patch-1"
    run["head_repository"] = {**run["head_repository"], "id": 777, "full_name": FORK}
    return payload


def fork_job(received_at: datetime, *, explain: bool) -> NewJob:
    """The job of the webhook for the fork run, or the one `/cicd-doctor explain` would add."""
    new_job = route_event("workflow_run", fork_payload(), received_at=received_at, max_attempts=5)
    assert new_job is not None
    if explain:
        return NewJob(
            kind=JobKind.DIAGNOSE_RUN,
            payload={
                **new_job.payload,
                "trigger": "explain",
                "pr_number": PR_NUMBER,
                "command_comment_id": 99,
            },
            dedupe_key="explain:99:5000:1",
        )
    return new_job


def fork_explain_job() -> NewJob:
    return fork_job(datetime.fromisoformat("2026-11-12T12:03:30+00:00"), explain=True)


async def enqueue(world: DiagnoseWorld, *, explain: bool) -> None:
    assert await world.queue.enqueue(fork_job(world.clock.now(), explain=explain), None)


async def test_explain_edits_the_check_run_and_the_comment_of_a_fork_run(
    world: DiagnoseWorld,
) -> None:
    world.add_run()
    world.writes.add_pull(PR_NUMBER, head_repo=FORK, head_ref="patch-1", head_sha=HEAD_SHA)
    await enqueue(world, explain=False)
    assert await run_job(world)
    [first_check] = world.requests("PATCH", CHECK)
    [posted] = world.requests("POST", COMMENTS)
    assert FALLBACK_TEXT["fork_policy"] in first_check.json["output"]["summary"]
    assert world.llm.calls == []
    downloads = len(world.requests("GET", LOG))

    world.llm.add(valid())
    await enqueue(world, explain=True)
    assert await run_job(world)

    assert len(world.llm.calls) == 1
    assert len(world.requests("GET", LOG)) == downloads  # no log downloaded again
    assert len(world.requests("GET", JOBS)) == 1  # nor the jobs listed
    assert len(world.requests("POST", CHECKS)) == 1  # the check run is not created again...
    assert [r.json["status"] for r in world.requests("PATCH", CHECK)] == ["completed", "completed"]
    assert len(world.requests("POST", COMMENTS)) == 1  # ...and neither is the comment
    [edit] = world.requests("PATCH", COMMENT)
    assert edit.json["body"].splitlines()[0] == posted.json["body"].splitlines()[0]
    assert FALLBACK_TEXT["fork_policy"] not in edit.json["body"]
    assert "LLM " in edit.json["body"]
    second = world.requests("PATCH", CHECK)[1].json["output"]["summary"]
    assert "LLM " in second
    assert FALLBACK_TEXT["fork_policy"] not in second
    assert len(world.writes.check_runs) == 1
    assert len(world.writes.comments) == 1
    explain_job = await world.queue.get(2)
    assert explain_job is not None
    assert explain_job.status is JobStatus.DONE
