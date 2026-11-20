"""The diagnosis flow on the Postgres adapters (P1c d2): queue, store, ledger and installations.

The GitHub side is ``FakeGitHub`` behind the real GitHub adapters and the LLM is ``FakeLLM``; what
is real here is the database. The rows are read back with plain SQL, not through the adapters.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import psycopg
import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import FakeEmbedder
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.models import DiagnoseRunPayload, JobStatus
from tests.diagnose_world import (
    INSTALLATION_ID,
    PR_NUMBER,
    REPO_ID,
    DiagnoseWorld,
    diagnose_world,
    job_id_for,
)
from tests.e2e.test_explain_upgrade_e2e import (
    FORK,
    HEAD_SHA,
    fork_explain_job,
)
from tests.e2e.test_explain_upgrade_e2e import (
    enqueue as enqueue_fork,
)
from tests.e2e.test_similar_e2e import add_similar_runs, assert_second_run_lists_the_first
from tests.pg import libpq_url

CHECKS = "/repos/{owner}/{repo}/check-runs"
CHECK = "/repos/{owner}/{repo}/check-runs/{check_run_id}"
COMMENTS = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
COMMENT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"


def rows(url: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        return conn.execute(query).fetchall()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))


@pytest.fixture
async def world(pg_clean: str, clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock, queue=queue, store=store, ledger=ledger, installations=installations
        ) as built,
    ):
        yield built


async def test_the_happy_path_leaves_every_row_behind(world: DiagnoseWorld, pg_clean: str) -> None:
    world.add_run()
    await world.enqueue()

    assert await world.worker.run_one("pg")

    job = await world.queue.get(1)
    assert job is not None
    assert (job.status, job.attempts) == (JobStatus.DONE, 1)
    assert rows(
        pg_clean, "SELECT id, repository_id, workflow_id, pr_number FROM workflow_runs"
    ) == [(5000, REPO_ID, 7, PR_NUMBER)]
    assert rows(pg_clean, "SELECT id, account_login FROM installations") == [
        (INSTALLATION_ID, "acme")
    ]
    [(run_id, attempt, analyzed)] = rows(
        pg_clean, "SELECT run_id, attempt, analyzed_at IS NOT NULL FROM run_attempts"
    )
    assert (run_id, attempt, analyzed) == (5000, 1, True)
    failures = rows(pg_clean, "SELECT run_id, run_attempt, job_id, status FROM failures")
    assert failures == [(5000, 1, job_id_for(5000, 1), "open")]
    diagnoses = rows(
        pg_clean, "SELECT source, published, category, rule_ids FROM diagnoses ORDER BY published"
    )
    # The rules baseline is kept (unpublished) next to the one published diagnosis.
    assert diagnoses == [
        ("rules", False, "dependency", ["PY-MODNF"]),
        ("llm", True, "dependency", []),
    ]
    [(check_run_id, last_attempt)] = rows(
        pg_clean, "SELECT check_run_id, last_attempt FROM check_publications"
    )
    assert last_attempt == 1
    assert check_run_id in world.writes.check_runs
    [(pr, workflow, comment_id, last_run, last_attempt)] = rows(
        pg_clean,
        "SELECT pr_number, workflow_id, comment_id, last_run_id, last_attempt "
        "FROM comment_publications",
    )
    assert (pr, workflow, last_run, last_attempt) == (PR_NUMBER, 7, 5000, 1)
    assert comment_id in world.writes.comments
    [(calls, reserved, spent)] = rows(
        pg_clean, "SELECT calls, reserved_usd, spent_usd FROM llm_usage"
    )
    assert calls == 1
    assert reserved == 0  # the reservation was settled
    assert spent > 0


async def test_the_embedding_goes_through_the_checkpoint_and_comes_back_equal(
    pg_clean: str, clock: FakeClock
) -> None:
    embedder = FakeEmbedder()
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock,
            queue=queue,
            store=store,
            ledger=ledger,
            installations=installations,
            embedder=embedder,
        ) as world,
    ):
        world.add_run()
        await world.enqueue()

        assert await world.worker.run_one("pg")

        record = await store.run_analysis(5000, 1)
        assert record is not None
        [item] = record.items
        expected = embedder.embed([item.failure.signature.text])[0]
        assert item.failure.embedding == pytest.approx(expected, abs=1e-6)  # float4 column
        assert rows(pg_clean, "SELECT vector_dims(embedding) FROM failures") == [(384,)]


async def test_attempt_two_is_published_by_patching_what_attempt_one_created(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    world.add_run(attempt=1)
    world.add_run(attempt=2)
    await world.enqueue(attempt=1)
    assert await world.worker.run_one("pg")
    await world.enqueue(attempt=2)

    assert await world.worker.run_one("pg")

    assert len(world.requests("POST", CHECKS)) == 1
    assert len(world.requests("POST", COMMENTS)) == 1
    assert len(world.requests("PATCH", COMMENT)) == 1
    assert len(world.writes.check_runs) == 1
    assert len(world.writes.comments) == 1
    assert rows(
        pg_clean,
        "SELECT run_id, attempt, analyzed_at IS NOT NULL FROM run_attempts ORDER BY attempt",
    ) == [(5000, 1, True), (5000, 2, True)]
    assert rows(pg_clean, "SELECT run_attempt, job_id FROM failures ORDER BY run_attempt") == [
        (1, job_id_for(5000, 1)),
        (2, job_id_for(5000, 2)),
    ]
    assert rows(pg_clean, "SELECT count(*) FROM diagnoses WHERE published") == [(2,)]
    assert rows(pg_clean, "SELECT last_attempt FROM check_publications") == [(2,)]
    assert rows(pg_clean, "SELECT last_run_id, last_attempt FROM comment_publications") == [
        (5000, 2)
    ]
    assert rows(pg_clean, "SELECT calls FROM llm_usage") == [(2,)]
    statuses = rows(pg_clean, "SELECT status, attempts FROM jobs ORDER BY id")
    assert statuses == [("done", 1), ("done", 1)]


async def test_an_identical_failure_in_the_next_run_is_served_from_the_cache(
    world: DiagnoseWorld, pg_clean: str
) -> None:
    world.add_run(5000, job_name="tests")
    world.add_run(5001, job_name="tests")
    await world.enqueue(5000)
    assert await world.worker.run_one("pg")
    await world.enqueue(5001)
    assert await world.worker.run_one("pg")

    assert len(world.llm.calls) == 1
    published = rows(
        pg_clean,
        "SELECT f.run_id, d.source, d.llm_calls, d.cached_from IS NOT NULL "
        "FROM diagnoses d JOIN failures f ON f.id = d.failure_id "
        "WHERE d.published ORDER BY f.run_id",
    )
    assert published == [(5000, "llm", 1, False), (5001, "cache", None, True)]
    # ``cached_from`` points at the llm row of run 5000.
    assert rows(
        pg_clean,
        "SELECT o.source, of.run_id FROM diagnoses d "
        "JOIN diagnoses o ON o.id = d.cached_from JOIN failures of ON of.id = o.failure_id "
        "WHERE d.source = 'cache'",
    ) == [("llm", 5000)]
    [(calls,)] = rows(pg_clean, "SELECT calls FROM llm_usage")
    assert calls == 1
    # What the second run published says where it came from, read back through the adapter.
    record = await world.store.run_analysis(5001, 1)
    assert record is not None
    assert (record.items[0].cached_from_run_id, record.items[0].published.source) == (5000, "cache")
    [check] = world.requests("PATCH", CHECK)[1:]
    assert "cached from run #5000" in check.json["output"]["summary"]


async def test_a_similar_failure_is_found_by_pgvector_and_listed_in_the_report(
    pg_clean: str, clock: FakeClock
) -> None:
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock,
            queue=queue,
            store=store,
            ledger=ledger,
            installations=installations,
            embedder=FakeEmbedder(),
        ) as world,
    ):
        add_similar_runs(world)
        await world.enqueue(5000)
        assert await world.worker.run_one("pg")
        await world.enqueue(5001)
        assert await world.worker.run_one("pg")

        assert_second_run_lists_the_first(world)


# --- explain upgrades what is published (P3 d1, D-94, D-95) -----------------------------------


@pytest.fixture
async def fork_world(pg_clean: str, clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    """Postgres everywhere and an LLM that has nothing to say until a test gives it a script."""
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock,
            queue=queue,
            store=store,
            ledger=ledger,
            installations=installations,
            llm_script=[],
        ) as built,
    ):
        yield built


async def _fork_run_with_rules(world: DiagnoseWorld) -> None:
    world.add_run()
    world.writes.add_pull(PR_NUMBER, head_repo=FORK, head_ref="patch-1", head_sha=HEAD_SHA)
    await enqueue_fork(world, explain=False)
    assert await world.worker.run_one("pg")


async def test_a_fork_run_is_upgraded_in_place_and_the_database_keeps_one_published_row(
    fork_world: DiagnoseWorld, pg_clean: str
) -> None:
    world = fork_world
    await _fork_run_with_rules(world)
    assert rows(pg_clean, "SELECT source, published FROM diagnoses") == [("rules", True)]
    world.llm.add(valid())

    await enqueue_fork(world, explain=True)
    assert await world.worker.run_one("pg")

    assert rows(
        pg_clean, "SELECT source, published, fallback_reason FROM diagnoses ORDER BY published"
    ) == [("rules", False, None), ("llm", True, None)]
    [(calls, reserved, spent)] = rows(
        pg_clean, "SELECT calls, reserved_usd, spent_usd FROM llm_usage"
    )
    assert (calls, reserved) == (1, 0)
    assert spent > 0
    [(cost,)] = rows(pg_clean, "SELECT cost_usd FROM diagnoses WHERE published")
    assert cost == spent  # the cost lives on the published row
    assert len(world.writes.check_runs) == 1
    assert len(world.writes.comments) == 1
    with (
        pytest.raises(psycopg.errors.UniqueViolation),  # the index of published rows
        psycopg.connect(libpq_url(pg_clean)) as conn,
    ):
        conn.execute(
            "INSERT INTO diagnoses (failure_id, source, published, category, probable_cause, "
            "evidence, evidence_verified, suggested_fix, confidence, rule_ids, created_at) "
            "SELECT failure_id, 'llm', true, category, probable_cause, evidence, true, "
            "suggested_fix, confidence, rule_ids, now() FROM diagnoses WHERE published"
        )
    # The upgraded row of a fork never feeds the cache or the similar failures of other runs.
    assert rows(
        pg_clean,
        "SELECT count(*) FROM diagnoses d JOIN failures f ON f.id = d.failure_id "
        "JOIN workflow_runs r ON r.id = f.run_id WHERE d.source = 'llm' AND NOT r.is_fork",
    ) == [(0,)]


async def test_two_explains_of_one_attempt_call_the_model_once_on_postgres(
    fork_world: DiagnoseWorld, pg_clean: str
) -> None:
    world = fork_world
    await _fork_run_with_rules(world)
    world.llm.add(valid(), valid())
    payload = DiagnoseRunPayload.model_validate(fork_explain_job().payload)

    await asyncio.gather(world.use_case.run(payload), world.use_case.run(payload))

    assert len(world.llm.calls) == 1
    [(calls,)] = rows(pg_clean, "SELECT calls FROM llm_usage")
    assert calls == 1
    assert rows(pg_clean, "SELECT source, published FROM diagnoses ORDER BY published") == [
        ("rules", False),
        ("llm", True),
    ]
