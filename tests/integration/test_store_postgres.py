"""What only the Postgres ``FailureStore`` can show (SPEC §6, §12, D-69).

The behaviour every adapter shares is in ``tests/contract/test_failure_store.py``. Here are the
database facts under it: the checkpoint is atomic, the first writer wins among many "processes",
the database itself refuses a second published diagnosis, and times come from the clock.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from sqlalchemy import make_url
from sqlalchemy.exc import DBAPIError

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Installation, Repository, RunRef
from tests.factories import (
    T0,
    make_failure,
    make_run_analysis,
    make_run_attempt,
    make_run_ref,
    make_stored_failure,
    make_workflow_run,
)
from tests.pg import libpq_url, scalar

RUN = make_run_ref()


@pytest.fixture
async def store(pg_clean: str) -> AsyncIterator[PostgresFailureStore]:
    clock = FakeClock(T0)
    async with PostgresInstallationStore.connect(pg_clean) as installations:
        await _prepare(installations, RUN)
    async with PostgresFailureStore.connect(pg_clean, clock) as postgres:
        yield postgres


async def _prepare(installations: PostgresInstallationStore, run: RunRef) -> None:
    await installations.save_installation(
        Installation(id=run.installation_id, account_login="acme", account_type="Organization")
    )
    await installations.save_repositories(
        [
            Repository(
                id=run.repo_id,
                installation_id=run.installation_id,
                full_name=run.repo_full_name,
                private=False,
                default_branch="main",
            )
        ]
    )


def _execute(url: str, query: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        conn.execute(query, params)


async def test_a_failed_save_leaves_no_checkpoint_and_the_retry_succeeds(
    pg_clean: str, store: PostgresFailureStore
) -> None:
    good = make_stored_failure(run=RUN, job_id=1)
    broken = make_stored_failure(run=RUN, job_id=2)
    # Postgres text cannot hold NUL: the insert of this failure fails after the others went in.
    broken_job = broken.failure.job.job.model_copy(update={"job_name": "te\x00sts"})
    broken = broken.model_copy(
        update={
            "failure": broken.failure.model_copy(
                update={"job": broken.failure.job.model_copy(update={"job": broken_job})}
            )
        }
    )

    with pytest.raises(DBAPIError):
        await store.save_run_analysis(make_run_analysis(RUN, [good, broken]))

    assert await store.run_analysis(RUN.run_id, RUN.run_attempt) is None
    assert scalar(pg_clean, "SELECT count(*) FROM failures") == 0
    assert scalar(pg_clean, "SELECT count(*) FROM diagnoses") == 0
    assert scalar(pg_clean, "SELECT count(*) FROM run_attempts WHERE analyzed_at IS NOT NULL") == 0
    record = make_run_analysis(RUN, [good])
    assert await store.save_run_analysis(record) == record  # the retry is not blocked by it


async def test_many_writers_of_one_attempt_store_exactly_one_analysis(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with PostgresInstallationStore.connect(pg_clean) as installations:
        await _prepare(installations, RUN)
    records = [
        make_run_analysis(
            RUN, [make_stored_failure(run=RUN, job_id=job) for job in range(w, w + 3)]
        )
        for w in range(1, 9)  # eight writers, eight different analyses of the same attempt
    ]
    async with (
        PostgresFailureStore.connect(pg_clean, clock) as first,
        PostgresFailureStore.connect(pg_clean, clock) as second,
    ):
        results = await asyncio.gather(
            *(
                (first if i % 2 == 0 else second).save_run_analysis(record)
                for i, record in enumerate(records)
            )
        )
        stored = await first.run_analysis(RUN.run_id, RUN.run_attempt)

    assert stored is not None
    assert stored in records
    assert all(result == stored for result in results)  # everybody continues with the winner
    assert scalar(pg_clean, "SELECT count(*) FROM failures") == 3
    assert scalar(pg_clean, "SELECT count(*) FROM diagnoses") == 3 * 2  # rules + llm each
    assert scalar(pg_clean, "SELECT count(*) FROM diagnoses WHERE published") == 3


async def test_the_database_refuses_a_second_published_diagnosis(
    pg_clean: str, store: PostgresFailureStore
) -> None:
    record = make_run_analysis(RUN, [make_stored_failure(run=RUN)])
    await store.save_run_analysis(record)
    failure_id = record.items[0].failure.id
    insert = """
        INSERT INTO diagnoses (failure_id, source, published, category, probable_cause, evidence,
                               suggested_fix, confidence)
        VALUES (%s, 'rules', %s, 'unknown', 'x', '[]', 'y', 0.5)
    """

    _execute(pg_clean, insert, (failure_id, False))  # more unpublished rows are fine
    with pytest.raises(psycopg.errors.UniqueViolation):
        _execute(pg_clean, insert, (failure_id, True))


async def test_the_times_come_from_the_clock(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with PostgresInstallationStore.connect(pg_clean) as installations:
        await _prepare(installations, RUN)
    first, second = make_run_ref(run_attempt=1), make_run_ref(run_attempt=2)
    async with PostgresFailureStore.connect(pg_clean, clock) as store:
        await store.save_run_analysis(make_run_analysis(first, [make_stored_failure(run=first)]))
        clock.advance(timedelta(hours=1))
        await store.save_run_analysis(make_run_analysis(second, [make_stored_failure(run=second)]))

    analysed = scalar(pg_clean, "SELECT analyzed_at FROM run_attempts WHERE attempt = 2")
    created = scalar(
        pg_clean,
        "SELECT min(created_at) FROM diagnoses WHERE failure_id IN "
        "(SELECT id FROM failures WHERE run_attempt = 2)",
    )
    assert analysed == T0 + timedelta(hours=1)
    assert created == T0 + timedelta(hours=1)
    assert scalar(pg_clean, "SELECT analyzed_at FROM run_attempts WHERE attempt = 1") == T0


async def test_the_attempt_keeps_its_own_conclusion_and_time(
    pg_clean: str, store: PostgresFailureStore
) -> None:
    record = make_run_analysis(RUN, [], conclusion="timed_out")

    stored = await store.save_run_analysis(record)

    assert stored.attempt == make_run_attempt(RUN, "timed_out")
    assert stored.run == make_workflow_run(RUN)


async def test_an_unreachable_database_is_a_transient_error(pg_clean: str) -> None:
    missing = make_url(pg_clean).set(database="cicd_doctor_does_not_exist")
    url = missing.render_as_string(hide_password=False)

    async with PostgresFailureStore.connect(url, FakeClock(T0)) as store:
        with pytest.raises(TransientError, match="database unavailable"):
            await store.run_analysis(1, 1)
        with pytest.raises(TransientError, match="database unavailable"):
            await store.save_failure(make_failure())
