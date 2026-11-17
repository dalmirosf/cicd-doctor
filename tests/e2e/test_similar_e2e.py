"""Offline end to end of the similar past failures (P2 d, D-82): two failures that are alike but
not identical (the missing module changes), two signed webhooks. The cache of D-81 does not serve
the second one, the model is called twice, and the second check run and the sticky comment list
the first run with a link and its score. No network and no Postgres."""

from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    FakeEmbedder,
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from tests.diagnose_world import LOG_BODY, DiagnoseWorld, diagnose_world, log_bytes
from tests.e2e.test_diagnose_e2e import CHECK, COMMENTS, deliver, run_job

COMMENT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"


@pytest.fixture
async def world() -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
        embedder=FakeEmbedder(),
    ) as built:
        yield built


def add_similar_runs(world: DiagnoseWorld) -> None:
    """Run 5000 misses ``requests``; run 5001 misses ``flask`` (a different signature)."""
    world.add_run(5000, job_name="tests")
    world.add_run(
        5001, job_name="tests", body=log_bytes([t.replace("requests", "flask") for t in LOG_BODY])
    )


def assert_second_run_lists_the_first(world: DiagnoseWorld) -> None:
    checks = world.requests("PATCH", CHECK)
    assert len(checks) == 2
    first, second = (c.json["output"]["summary"] for c in checks)
    assert "Similar past" not in first
    assert "[run #5000](https://github.com/acme/widgets/actions/runs/5000)" in second
    assert "cached from" not in second  # not identical: the model was asked
    assert "(score 0.9" in second or "(score 1.00" in second
    [edited] = world.requests("PATCH", COMMENT)
    assert "run #5000" in edited.json["body"]
    assert len(world.requests("POST", COMMENTS)) == 1
    assert len(world.llm.calls) == 2
    assert world.llm.calls[0].similar == []
    assert [s.run_id for s in world.llm.calls[1].similar] == [5000]


async def test_the_second_similar_run_lists_the_first_with_a_link_and_a_score(
    world: DiagnoseWorld,
) -> None:
    add_similar_runs(world)
    assert deliver(world, 5000, delivery="d1d2d3d4-0000-4000-8000-000000000001") == 202
    assert await run_job(world)
    assert deliver(world, 5001, delivery="d1d2d3d4-0000-4000-8000-000000000002") == 202
    assert await run_job(world)

    assert_second_run_lists_the_first(world)
