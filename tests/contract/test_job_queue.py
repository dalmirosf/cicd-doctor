"""Contract suite for the ``JobQueue`` port (SPEC §6, §12, D-47, D-48).

Every adapter must pass these tests with the same ``FakeClock``. The ``postgres`` parameter is
marked ``integration`` (D-51), so the fast suite runs only the in-memory adapter.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.core.models import Delivery, Job, JobKind, JobStatus, NewJob
from cicd_doctor.core.ports import JobQueue
from cicd_doctor.core.retry import LAST_ERROR_MAX_BYTES
from tests.factories import T0
from tests.pg import scalar

LEASE = timedelta(seconds=300)


@dataclass
class Harness:
    queue: JobQueue
    clock: FakeClock
    get: Callable[[int], Awaitable[Job | None]]
    delivery_count: Callable[[], int]


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    clock = FakeClock(T0)
    if request.param == "memory":
        memory = InMemoryJobQueue(clock)
        yield Harness(memory, clock, memory.get, lambda: len(memory.deliveries))
        return
    url: str = request.getfixturevalue("pg_clean")
    async with PostgresJobQueue.connect(url, clock) as postgres:
        yield Harness(
            postgres,
            clock,
            postgres.get,
            lambda: int(str(scalar(url, "SELECT count(*) FROM webhook_deliveries"))),
        )


def new_job(**changes: Any) -> NewJob:
    fields: dict[str, Any] = {"kind": JobKind.DIAGNOSE_RUN, "payload": {"run_id": 1}}
    return NewJob(**(fields | changes))


def delivery() -> Delivery:
    return Delivery(
        delivery_id=uuid4(), event="workflow_run", action="completed", installation_id=1
    )


async def lease(h: Harness, worker: str = "w1") -> Job:
    job = await h.queue.lease(worker, LEASE)
    assert job is not None
    return job


async def stored(h: Harness, job: Job) -> Job:
    current = await h.get(job.id)
    assert current is not None
    return current


# --- enqueue and idempotency (D-06) -------------------------------------------------------


async def test_lease_returns_none_on_an_empty_queue(harness: Harness) -> None:
    assert await harness.queue.lease("w1", LEASE) is None


async def test_a_leased_job_is_running_with_one_attempt_and_a_lease(harness: Harness) -> None:
    payload = {"run_id": 42, "nested": {"attempts": [1, 2]}, "name": "ci"}
    assert await harness.queue.enqueue(new_job(payload=payload), None) is True

    job = await lease(harness, "worker-a")

    assert job.kind is JobKind.DIAGNOSE_RUN
    assert job.payload == payload
    assert job.status is JobStatus.RUNNING
    assert job.attempts == 1
    assert job.locked_by == "worker-a"
    assert job.locked_until == T0 + LEASE
    assert job.run_after == T0


async def test_the_same_delivery_three_times_creates_one_delivery_and_one_job(
    harness: Harness,
) -> None:
    same = delivery()
    job = new_job(dedupe_key="diagnose:1:1")

    results = [await harness.queue.enqueue(job, same) for _ in range(3)]

    assert results == [True, False, False]
    assert harness.delivery_count() == 1
    await lease(harness)
    assert await harness.queue.lease("w2", LEASE) is None


async def test_a_duplicate_dedupe_key_is_rejected_even_from_another_delivery(
    harness: Harness,
) -> None:
    assert await harness.queue.enqueue(new_job(dedupe_key="diagnose:1:1"), delivery()) is True
    assert await harness.queue.enqueue(new_job(dedupe_key="diagnose:1:1"), delivery()) is False

    await lease(harness)
    assert await harness.queue.lease("w2", LEASE) is None


async def test_jobs_without_a_dedupe_key_never_collide(harness: Harness) -> None:
    assert await harness.queue.enqueue(new_job(), None) is True
    assert await harness.queue.enqueue(new_job(), None) is True

    first, second = await lease(harness), await lease(harness)

    assert first.id != second.id


# --- leasing ------------------------------------------------------------------------------


async def test_a_job_is_not_leased_before_its_run_after(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(run_after=T0 + timedelta(minutes=1)), None)

    assert await harness.queue.lease("w1", LEASE) is None
    harness.clock.advance(timedelta(minutes=1))
    assert (await lease(harness)).run_after == T0 + timedelta(minutes=1)


async def test_jobs_are_leased_in_run_after_order(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(payload={"n": "late"}, run_after=T0), None)
    await harness.queue.enqueue(new_job(payload={"n": "early"}, run_after=T0 - timedelta(1)), None)

    assert (await lease(harness)).payload == {"n": "early"}
    assert (await lease(harness)).payload == {"n": "late"}


async def test_a_job_is_not_leased_again_while_its_lease_is_valid(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(), None)
    await lease(harness, "w1")

    harness.clock.advance(LEASE)  # locked_until == now: still valid (it must be < now)

    assert await harness.queue.lease("w2", LEASE) is None


async def test_an_expired_lease_is_taken_by_another_worker_as_a_new_attempt(
    harness: Harness,
) -> None:
    await harness.queue.enqueue(new_job(), None)
    first = await lease(harness, "w1")

    harness.clock.advance(LEASE + timedelta(seconds=1))
    second = await lease(harness, "w2")

    assert second.id == first.id
    assert second.attempts == 2
    assert second.locked_by == "w2"


async def test_heartbeat_extends_the_lease(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(), None)
    job = await lease(harness, "w1")

    harness.clock.advance(timedelta(seconds=200))
    assert await harness.queue.heartbeat(job, LEASE) is True
    harness.clock.advance(timedelta(seconds=200))  # past the original lease, inside the new one

    assert await harness.queue.lease("w2", LEASE) is None
    assert (await stored(harness, job)).locked_until == T0 + timedelta(seconds=200) + LEASE


async def test_a_stale_lease_changes_nothing(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(), None)
    stale = await lease(harness, "w1")
    harness.clock.advance(LEASE + timedelta(seconds=1))
    current = await lease(harness, "w2")

    assert await harness.queue.heartbeat(stale, LEASE) is False
    assert await harness.queue.complete(stale) is False
    assert await harness.queue.retry(stale, T0, "boom") is False
    assert await harness.queue.fail(stale, "boom", dead=True) is False

    assert await stored(harness, current) == current


# --- outcomes -----------------------------------------------------------------------------


async def test_a_completed_job_is_done_and_never_leased_again(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(), None)
    job = await lease(harness)

    assert await harness.queue.complete(job) is True
    harness.clock.advance(LEASE * 2)

    assert (await stored(harness, job)).status is JobStatus.DONE
    assert await harness.queue.lease("w2", LEASE) is None
    assert await harness.queue.complete(job) is False


async def test_retry_requeues_the_job_at_run_after_with_its_error(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(), None)
    job = await lease(harness)
    run_after = T0 + timedelta(seconds=30)

    assert await harness.queue.retry(job, run_after, "TransientError: 502") is True

    queued = await stored(harness, job)
    assert queued.status is JobStatus.QUEUED
    assert queued.run_after == run_after
    assert queued.last_error == "TransientError: 502"
    assert queued.locked_until is None
    assert await harness.queue.lease("w1", LEASE) is None
    harness.clock.advance(timedelta(seconds=30))
    assert (await lease(harness)).attempts == 2


async def test_last_error_is_truncated_to_2_kb_without_splitting_characters(
    harness: Harness,
) -> None:
    await harness.queue.enqueue(new_job(), None)
    job = await lease(harness)

    await harness.queue.retry(job, T0, "é" * 5000)  # 2 bytes per character

    error = (await stored(harness, job)).last_error
    assert error is not None
    assert len(error.encode("utf-8")) == LAST_ERROR_MAX_BYTES
    assert set(error) == {"é"}


@pytest.mark.parametrize(("dead", "status"), [(False, JobStatus.FAILED), (True, JobStatus.DEAD)])
async def test_fail_marks_the_job_failed_or_dead(
    harness: Harness, dead: bool, status: JobStatus
) -> None:
    await harness.queue.enqueue(new_job(), None)
    job = await lease(harness)

    assert await harness.queue.fail(job, "PermanentError: 404", dead=dead) is True
    harness.clock.advance(LEASE * 2)

    failed = await stored(harness, job)
    assert failed.status is status
    assert failed.last_error == "PermanentError: 404"
    assert await harness.queue.lease("w2", LEASE) is None


async def test_a_job_whose_last_attempt_never_finished_becomes_dead(harness: Harness) -> None:
    await harness.queue.enqueue(new_job(max_attempts=2), None)
    await lease(harness, "w1")
    harness.clock.advance(LEASE + timedelta(seconds=1))
    last = await lease(harness, "w2")
    assert last.attempts == 2
    harness.clock.advance(LEASE + timedelta(seconds=1))

    assert await harness.queue.lease("w3", LEASE) is None

    dead = await stored(harness, last)
    assert dead.status is JobStatus.DEAD
    assert dead.attempts == 2
    assert dead.last_error is not None


# --- wake-up ------------------------------------------------------------------------------


async def test_wait_for_jobs_times_out_without_new_jobs(harness: Harness) -> None:
    assert await harness.queue.wait_for_jobs(timedelta(milliseconds=50)) is False


async def test_wait_for_jobs_wakes_up_on_enqueue(harness: Harness) -> None:
    waiter = asyncio.create_task(harness.queue.wait_for_jobs(timedelta(seconds=5)))
    await asyncio.sleep(0.05)  # let the waiter register

    await harness.queue.enqueue(new_job(), None)

    async with asyncio.timeout(2):
        assert await waiter is True
