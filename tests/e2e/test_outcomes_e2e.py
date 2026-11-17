"""Offline end to end of the success path (P2 e2a, D-83): a signed ``success`` webhook becomes a
``record_outcome`` job that resolves the failure of the branch in the store. GitHub is not called
(the comment is rewritten in e2b). No network and no Postgres."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.core.models import FailureStatus
from tests.diagnose_world import REPO_ID, DiagnoseWorld, diagnose_world, workflow_run_payload
from tests.e2e.test_diagnose_e2e import (
    CHECK,
    CHECKS,
    COMMENT,
    COMMENTS,
    TOKEN,
    WEBHOOK,
    deliver,
    run_job,
)
from tests.webhooks import SECRET, encode, headers

FIX_SHA = "b" * 40


@pytest.fixture
async def world() -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
    ) as built:
        yield built


async def _ready() -> bool:
    return True


def deliver_success(
    world: DiagnoseWorld, run_id: int, attempt: int = 1, *, delivery: str, head_sha: str = FIX_SHA
) -> int:
    deps = ApiDependencies(
        queue=world.queue, webhook_secrets=(SECRET,), ready=_ready, clock=world.clock
    )
    body = encode(workflow_run_payload(run_id, attempt, "success", head_sha))
    with TestClient(create_app(deps)) as client:
        response = client.post(
            WEBHOOK, content=body, headers=headers("workflow_run", body, delivery=delivery)
        )
    return response.status_code


async def status_of(world: DiagnoseWorld, run_id: int = 5000) -> tuple[FailureStatus, Any]:
    record = await world.store.run_analysis(run_id, 1)
    assert record is not None
    [item] = record.items
    return item.failure.status, item.failure.fix_commit_sha


async def test_a_signed_failure_then_a_signed_success_rewrites_the_same_comment(
    world: DiagnoseWorld,
) -> None:
    world.add_run(5000)
    assert deliver(world, 5000, delivery="e1000000-0000-4000-8000-000000000001") == 202
    assert await run_job(world)
    assert await status_of(world) == (FailureStatus.OPEN, None)
    [posted] = world.requests("POST", COMMENTS)
    assert "Likely cause" in posted.json["body"]
    [first_check] = world.requests("PATCH", CHECK)
    llm_calls = len(world.llm.calls)

    assert deliver_success(world, 5001, delivery="e1000000-0000-4000-8000-000000000002") == 202
    assert await run_job(world)

    assert await status_of(world) == (FailureStatus.RESOLVED_BY_COMMIT, FIX_SHA)
    [posted_again] = world.requests("POST", COMMENTS)  # still exactly one comment created
    assert posted_again is posted
    [edited] = world.requests("PATCH", COMMENT)  # ...and that one comment was edited in place
    assert "**✅ Fixed by `bbbbbbb`**" in edited.json["body"]
    assert "Likely cause" not in edited.json["body"]
    assert world.requests("PATCH", CHECK) == [first_check]  # the check run was not touched
    assert len(world.requests("POST", CHECKS)) == 1
    assert len(world.llm.calls) == llm_calls
    assert len(world.writes.comments) == 1
    [comment] = world.writes.comments.values()
    assert "✅ Fixed by" in comment["body"]
    token_bodies = [r.json for r in world.requests("POST", TOKEN)]
    # The rewrite asks for no permission the diagnosis does not have (D-84).
    assert all(
        b["permissions"] == {"actions": "read", "checks": "write", "pull_requests": "write"}
        for b in token_bodies
    )


async def test_a_signed_success_then_the_late_failure_ends_resolved(world: DiagnoseWorld) -> None:
    world.add_run(5000)
    assert deliver_success(world, 5001, delivery="e1000000-0000-4000-8000-000000000003") == 202
    assert await run_job(world)
    assert world.calls() == []  # nothing to resolve and nothing to write: no GitHub call at all

    assert deliver(world, 5000, delivery="e1000000-0000-4000-8000-000000000004") == 202
    assert await run_job(world)

    assert await status_of(world) == (FailureStatus.RESOLVED_BY_COMMIT, FIX_SHA)
    [posted] = world.requests("POST", COMMENTS)
    assert "**✅ Fixed by `bbbbbbb`**" in posted.json["body"]
    assert "Likely cause" not in posted.json["body"]
    [check] = world.requests("PATCH", CHECK)
    assert check.json["output"]["title"] == "✅ Fixed by bbbbbbb"


async def test_a_redelivered_success_makes_no_second_job(world: DiagnoseWorld) -> None:
    first = deliver_success(world, 5001, delivery="e1000000-0000-4000-8000-000000000005")
    second = deliver_success(world, 5001, delivery="e1000000-0000-4000-8000-000000000006")

    assert (first, second) == (202, 200)  # the duplicate delivery is acknowledged, not queued
    assert await run_job(world) is True
    assert await run_job(world) is False  # nothing else was queued


async def test_a_skipped_run_is_ignored(world: DiagnoseWorld) -> None:
    deps = ApiDependencies(
        queue=world.queue, webhook_secrets=(SECRET,), ready=_ready, clock=world.clock
    )
    body = encode(workflow_run_payload(5001, 1, "skipped"))

    with TestClient(create_app(deps)) as client:
        response = client.post(WEBHOOK, content=body, headers=headers("workflow_run", body))

    assert response.status_code == 204
    assert await run_job(world) is False
    assert REPO_ID == 100
