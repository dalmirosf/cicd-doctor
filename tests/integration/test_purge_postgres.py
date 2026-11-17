"""The purge on the real database (D-85): volume, cascade, batches and two purges at once.

The behaviour every adapter shares is in the contract suites. Here are the database facts: many
rows go in batches of their own transactions, the cascade leaves no orphans, a writer is not
blocked between batches, and two purges that run at the same time (two engines, as two workers)
neither deadlock nor leave a run behind.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import psycopg
import pytest
from sqlalchemy import event

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.models import Installation, Repository
from cicd_doctor.core.usecases.purge import PurgeConfig, run_purge
from tests.factories import T0
from tests.pg import libpq_url, scalar

DAY = timedelta(days=1)
OLD = T0 - 91 * DAY
NEW = T0 - 10 * DAY
CUT = T0 - 90 * DAY
BATCH = 700  # 5 000 rows are 8 batches


@pytest.fixture
def url(pg_clean: str) -> str:
    with psycopg.connect(libpq_url(pg_clean), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO installations (id, account_login, account_type) VALUES (1, 'acme', 'User')"
        )
        for repo_id in (100, 200):
            conn.execute(
                "INSERT INTO repositories (id, installation_id, full_name, private, default_branch)"
                " VALUES (%s, 1, %s, false, 'main')",
                (repo_id, f"acme/repo-{repo_id}"),
            )
    return pg_clean


def seed(
    url: str, *, first_run: int, runs: int, attempts: int, completed_at: datetime, repo: int = 100
) -> None:
    """``runs`` runs with ``attempts`` attempts each, a failure per attempt and a diagnosis per
    failure, all completed at ``completed_at``."""
    params = {
        "first": first_run,
        "last": first_run + runs - 1,
        "attempts": attempts,
        "at": completed_at,
        "repo": repo,
    }
    with psycopg.connect(libpq_url(url), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs (id, repository_id, workflow_id, workflow_name, head_sha,
                                       head_branch, head_repo_full_name, event, is_fork, created_at)
            SELECT g, %(repo)s, 7, 'CI', repeat('a', 40), 'main', 'acme/widgets', 'push', false,
                   %(at)s
            FROM generate_series(%(first)s::bigint, %(last)s::bigint) g
            """,
            params,
        )
        conn.execute(
            """
            INSERT INTO run_attempts (run_id, attempt, conclusion, completed_at)
            SELECT r, a, 'failure', %(at)s
            FROM generate_series(%(first)s::bigint, %(last)s::bigint) r,
                 generate_series(1, %(attempts)s) a
            """,
            params,
        )
        conn.execute(
            """
            INSERT INTO failures (repository_id, run_id, run_attempt, job_id, job_name,
                                  signature_hash, signature_text, excerpt, excerpt_tokens,
                                  conclusion, started_at, completed_at, created_at)
            SELECT %(repo)s, run_id, attempt, 1, 'tests', repeat('f', 64), 'sig', '{}'::jsonb, 1,
                   'failure', %(at)s, %(at)s, %(at)s
            FROM run_attempts WHERE run_id BETWEEN %(first)s AND %(last)s
            """,
            params,
        )
        conn.execute(
            """
            INSERT INTO diagnoses (failure_id, source, published, category, probable_cause,
                                   evidence, suggested_fix, confidence)
            SELECT id, 'rules', true, 'unknown', 'cause', '[]'::jsonb, 'fix', 0.5
            FROM failures WHERE run_id BETWEEN %(first)s AND %(last)s
            """,
            params,
        )


def count(url: str, table: str) -> int:
    return int(str(scalar(url, f"SELECT count(*) FROM {table}")))


def orphans(url: str) -> dict[str, int]:
    """Rows that lost their parent, and runs that lost all their attempts."""
    queries = {
        "failures": (
            "SELECT count(*) FROM failures f WHERE NOT EXISTS "
            "(SELECT 1 FROM run_attempts a WHERE a.run_id = f.run_id AND a.attempt = f.run_attempt)"
        ),
        "diagnoses": (
            "SELECT count(*) FROM diagnoses d WHERE NOT EXISTS "
            "(SELECT 1 FROM failures f WHERE f.id = d.failure_id)"
        ),
        "runs_without_attempts": (
            "SELECT count(*) FROM workflow_runs r WHERE NOT EXISTS "
            "(SELECT 1 FROM run_attempts a WHERE a.run_id = r.id)"
        ),
    }
    return {name: int(str(scalar(url, query))) for name, query in queries.items()}


@pytest.fixture
async def stores(url: str) -> AsyncIterator[tuple[PostgresFailureStore, PostgresFailureStore]]:
    """Two stores on two engines: two workers."""
    first, second = make_engine(url), make_engine(url)
    clock = FakeClock(T0)
    try:
        yield (
            PostgresFailureStore(first, clock, purge_batch=BATCH),
            PostgresFailureStore(second, clock, purge_batch=BATCH),
        )
    finally:
        await first.dispose()
        await second.dispose()


async def test_5000_old_rows_go_and_50_new_ones_stay_without_orphans(
    url: str, stores: tuple[PostgresFailureStore, PostgresFailureStore]
) -> None:
    seed(url, first_run=1, runs=5000, attempts=1, completed_at=OLD)
    seed(url, first_run=10_000, runs=50, attempts=1, completed_at=NEW)

    counts = await stores[0].purge(CUT, CUT)

    assert counts.deleted == {
        "diagnoses": 5000,
        "failures": 5000,
        "run_attempts": 5000,
        "workflow_runs": 5000,
        "publications": 0,
    }
    assert [count(url, t) for t in ("workflow_runs", "run_attempts", "failures", "diagnoses")] == [
        50,
        50,
        50,
        50,
    ]
    assert orphans(url) == {"failures": 0, "diagnoses": 0, "runs_without_attempts": 0}
    assert (await stores[0].purge(CUT, CUT)).total == 0


async def test_every_batch_is_its_own_transaction_and_writers_are_not_blocked(
    url: str, stores: tuple[PostgresFailureStore, PostgresFailureStore]
) -> None:
    seed(url, first_run=1, runs=5000, attempts=1, completed_at=OLD)
    commits: list[int] = []
    event.listen(
        stores[0]._engine.sync_engine,
        "commit",
        lambda conn: commits.append(1),
    )

    async def write_between_batches() -> int:
        """Another connection writes while the purge runs; it must never wait for it."""
        written = 0
        while not purge.done():
            seed(url, first_run=20_000 + written, runs=1, attempts=1, completed_at=NEW)
            written += 1
            await asyncio.sleep(0)
        return written

    purge = asyncio.ensure_future(stores[0].purge(CUT, CUT))
    async with asyncio.timeout(60):
        written, counts = await asyncio.gather(write_between_batches(), purge)

    assert counts.deleted["run_attempts"] == 5000
    assert len(commits) >= 5000 // BATCH + 1  # one commit per batch, never one for the whole purge
    assert written >= 1
    assert count(url, "run_attempts") == written


async def test_two_purges_at_once_do_not_deadlock_and_delete_every_row_exactly_once(
    url: str, stores: tuple[PostgresFailureStore, PostgresFailureStore]
) -> None:
    seed(url, first_run=1, runs=2500, attempts=2, completed_at=OLD)  # 5 000 attempts
    seed(url, first_run=10_000, runs=25, attempts=2, completed_at=NEW)  # 50 attempts

    async with asyncio.timeout(60):
        first, second = await asyncio.gather(stores[0].purge(CUT, CUT), stores[1].purge(CUT, CUT))

    total = first + second
    assert total.deleted["run_attempts"] == 5000
    assert total.deleted["failures"] == 5000
    assert total.deleted["diagnoses"] == 5000
    assert total.deleted["workflow_runs"] == 2500
    assert [count(url, t) for t in ("workflow_runs", "run_attempts", "failures", "diagnoses")] == [
        25,
        50,
        50,
        50,
    ]
    assert orphans(url) == {"failures": 0, "diagnoses": 0, "runs_without_attempts": 0}


async def test_a_purge_and_a_repository_purge_at_once_do_not_deadlock(
    url: str, stores: tuple[PostgresFailureStore, PostgresFailureStore]
) -> None:
    seed(url, first_run=1, runs=1500, attempts=2, completed_at=OLD, repo=100)
    seed(url, first_run=10_000, runs=1500, attempts=2, completed_at=NEW, repo=200)
    seed(url, first_run=20_000, runs=1500, attempts=2, completed_at=OLD, repo=200)

    async with asyncio.timeout(60):
        await asyncio.gather(stores[0].purge(CUT, CUT), stores[1].purge_repositories([100]))

    assert count(url, "workflow_runs") == 1500  # repo 200's recent runs
    assert orphans(url) == {"failures": 0, "diagnoses": 0, "runs_without_attempts": 0}


async def test_mark_stale_works_in_batches(
    url: str, stores: tuple[PostgresFailureStore, PostgresFailureStore]
) -> None:
    seed(url, first_run=1, runs=2000, attempts=1, completed_at=OLD)
    seed(url, first_run=10_000, runs=10, attempts=1, completed_at=NEW)

    changed = await stores[0].mark_stale(T0 - 30 * DAY, T0)

    assert changed == 2000
    assert int(str(scalar(url, "SELECT count(*) FROM failures WHERE status = 'open'"))) == 10
    assert int(str(scalar(url, "SELECT count(*) FROM failures WHERE resolved_at IS NOT NULL"))) == (
        2000
    )


async def test_the_queue_purge_deletes_in_batches(url: str) -> None:
    with psycopg.connect(libpq_url(url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO webhook_deliveries (delivery_id, event, received_at) "
            "SELECT gen_random_uuid(), 'workflow_run', %s FROM generate_series(1, 2500)",
            (T0 - 31 * DAY,),
        )
        conn.execute(
            "INSERT INTO webhook_deliveries (delivery_id, event, received_at) "
            "SELECT gen_random_uuid(), 'workflow_run', %s FROM generate_series(1, 20)",
            (T0 - 29 * DAY,),
        )
        conn.execute(
            "INSERT INTO jobs (kind, payload, status, created_at, updated_at) "
            "SELECT 'explain', '{}'::jsonb, 'done', %s, %s FROM generate_series(1, 2500)",
            (T0 - 31 * DAY, T0 - 31 * DAY),
        )
        conn.execute(
            "INSERT INTO jobs (kind, payload, status, created_at, updated_at) "
            "VALUES ('explain', '{}'::jsonb, 'queued', %s, %s)",
            (T0 - 31 * DAY, T0 - 31 * DAY),
        )
    engine = make_engine(url)
    try:
        queue = PostgresJobQueue(engine, FakeClock(T0), purge_batch=BATCH)
        counts = await queue.purge(T0 - 30 * DAY, T0 - 30 * DAY)
    finally:
        await engine.dispose()

    assert counts.deleted == {"jobs": 2500, "webhook_deliveries": 2500}
    assert count(url, "webhook_deliveries") == 20
    assert count(url, "jobs") == 1  # the queued one is never touched


async def test_run_purge_applies_every_rule_on_the_real_database(url: str) -> None:
    clock = FakeClock(T0)
    seed(url, first_run=1, runs=10, attempts=1, completed_at=T0 - 91 * DAY, repo=100)
    seed(url, first_run=100, runs=10, attempts=1, completed_at=T0 - 5 * DAY, repo=200)
    async with (
        PostgresJobQueue.connect(url, clock, listen=False) as queue,
        PostgresFailureStore.connect(url, clock) as store,
        PostgresInstallationStore.connect(url) as installations,
    ):
        await installations.remove_repositories([200], T0 - 8 * DAY)

        counts = await run_purge(queue, store, installations, clock, PurgeConfig())

        # 10 runs by age (91 days) and 10 more because repository 200 was removed 8 days ago
        assert counts.deleted["workflow_runs"] == 20
        assert counts.deleted["run_attempts"] == 20
        assert await installations.repository(200) is not None  # the rows of repositories stay
        assert await installations.installation(1) == Installation(
            id=1, account_login="acme", account_type="User"
        )
        assert await installations.repository(100) == Repository(
            id=100,
            installation_id=1,
            full_name="acme/repo-100",
            private=False,
            default_branch="main",
        )
        again = await run_purge(queue, store, installations, clock, PurgeConfig())
        assert again.total == 0
