"""Offline end to end of the exact-signature cache (P2 c, D-81): two identical failures, two
signed webhooks. The second run is published from the store: the check run and the comment say
"cached from run #N", and the model is called once. No network and no Postgres."""

from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from tests.diagnose_world import DiagnoseWorld, diagnose_world
from tests.e2e.test_diagnose_e2e import CHECK, COMMENTS, deliver, run_job


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


async def test_the_second_identical_run_says_where_its_diagnosis_came_from(
    world: DiagnoseWorld,
) -> None:
    world.add_run(5000, job_name="tests")
    world.add_run(5001, job_name="tests")
    assert deliver(world, 5000, delivery="c1c2d3e4-0000-4000-8000-000000000001") == 202
    assert await run_job(world)
    [first_check] = world.requests("PATCH", CHECK)
    assert "LLM " in first_check.json["output"]["summary"]
    assert "cached from" not in first_check.json["output"]["summary"]

    assert deliver(world, 5001, delivery="c1c2d3e4-0000-4000-8000-000000000002") == 202
    assert await run_job(world)

    checks = world.requests("PATCH", CHECK)
    assert len(checks) == 2
    assert "cached from run #5000" in checks[1].json["output"]["summary"]
    # One sticky comment, updated by the second run with the same note.
    [posted] = world.requests("POST", COMMENTS)
    assert "LLM " in posted.json["body"]
    [edited] = world.requests("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}")
    assert "cached from run #5000" in edited.json["body"]
    assert len(world.llm.calls) == 1
