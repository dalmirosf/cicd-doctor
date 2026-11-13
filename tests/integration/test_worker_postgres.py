"""The worker against a real Postgres: retries, heartbeats, concurrency and NOTIFY wake-ups."""

import asyncio
from collections import Counter
from datetime import timedelta

from cicd_doctor.adapters.clock import FakeClock, SystemClock
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.core.models import Job, JobKind, JobStatus, NewJob
from cicd_doctor.worker import Handler, Worker
from tests.factories import T0
from tests.pg import scalar
from tests.scenarios import assert_transient_errors_back_off_until_dead

KIND = JobKind.DIAGNOSE_RUN


def _worker(queue: PostgresJobQueue, handler: Handler, name: str, **options: object) -> Worker:
    settings: dict[str, object] = {
        "concurrency": 4,
        "lease": timedelta(seconds=5),
        "poll_interval": timedelta(seconds=0.2),
        "name": name,
    } | options
    return Worker(queue, {KIND: handler}, SystemClock(), **settings)  # type: ignore[arg-type]


async def test_transient_errors_back_off_until_dead_with_a_fake_clock(pg_clean: str) -> None:
    clock = FakeClock(T0)
    async with PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue:
        await assert_transient_errors_back_off_until_dead(queue, clock, queue.get)


async def test_heartbeats_keep_a_job_longer_than_its_lease_from_running_twice(
    pg_clean: str,
) -> None:
    runs: list[int] = []
    finished = asyncio.Event()

    async def slow(job: Job) -> None:
        runs.append(job.attempts)
        await asyncio.sleep(3)  # three times the lease
        finished.set()

    async with (
        PostgresJobQueue.connect(pg_clean, SystemClock()) as first,
        PostgresJobQueue.connect(pg_clean, SystemClock()) as second,
    ):
        await first.enqueue(NewJob(kind=KIND, payload={}), None)
        stop = asyncio.Event()
        workers = [
            asyncio.create_task(_worker(q, slow, n, lease=timedelta(seconds=1)).run(stop))
            for q, n in ((first, "a"), (second, "b"))
        ]
        try:
            await asyncio.wait_for(finished.wait(), 15)
        finally:
            stop.set()
            await asyncio.gather(*workers)
        done = await first.get(1)

    assert runs == [1]
    assert done is not None
    assert (done.status, done.attempts) == (JobStatus.DONE, 1)


async def test_concurrent_workers_run_every_job_exactly_once(pg_clean: str) -> None:
    total = 50
    seen: Counter[int] = Counter()
    all_done = asyncio.Event()

    async def handler(job: Job) -> None:
        await asyncio.sleep(0.01)
        seen[job.id] += 1
        if sum(seen.values()) >= total:
            all_done.set()

    async with (
        PostgresJobQueue.connect(pg_clean, SystemClock()) as first,
        PostgresJobQueue.connect(pg_clean, SystemClock()) as second,
    ):
        for n in range(total):
            await first.enqueue(NewJob(kind=KIND, payload={"n": n}), None)
        stop = asyncio.Event()
        workers = [
            asyncio.create_task(_worker(q, handler, n).run(stop))
            for q, n in ((first, "a"), (second, "b"))
        ]
        try:
            await asyncio.wait_for(all_done.wait(), 30)
            await asyncio.sleep(0.5)  # room for a duplicate to show up
        finally:
            stop.set()
            await asyncio.gather(*workers)

    assert sorted(seen) == list(range(1, total + 1))
    assert set(seen.values()) == {1}
    assert scalar(pg_clean, "SELECT count(*) FROM jobs WHERE status = 'done'") == total


async def test_notify_wakes_an_idle_worker_long_before_its_next_poll(pg_clean: str) -> None:
    handled = asyncio.Event()

    async def handler(job: Job) -> None:
        handled.set()

    async with PostgresJobQueue.connect(pg_clean, SystemClock()) as queue:
        stop = asyncio.Event()
        worker = _worker(queue, handler, "idle", poll_interval=timedelta(seconds=60))
        running = asyncio.create_task(worker.run(stop))
        try:
            await asyncio.sleep(0.5)  # every consumer is now waiting on LISTEN
            loop = asyncio.get_running_loop()
            enqueued_at = loop.time()
            await queue.enqueue(NewJob(kind=KIND, payload={}), None)
            await asyncio.wait_for(handled.wait(), 5)
            latency = loop.time() - enqueued_at
        finally:
            stop.set()
            await running

    assert latency < 2
