"""Worker behaviour with the in-memory queue and a fake clock (SPEC §8, D-23, D-49)."""

import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.core.errors import PermanentError, TransientError
from cicd_doctor.core.models import Job, JobKind, JobStatus, NewJob
from cicd_doctor.worker import Handler, Worker
from tests.factories import T0
from tests.scenarios import assert_transient_errors_back_off_until_dead

KIND = JobKind.DIAGNOSE_RUN


def make_worker(
    queue: InMemoryJobQueue,
    clock: FakeClock,
    handler: Handler,
    **options: object,
) -> Worker:
    settings: dict[str, object] = {"name": "test", "jitter": lambda: 1.0} | options
    return Worker(queue, {KIND: handler}, clock, **settings)  # type: ignore[arg-type]


async def enqueue(queue: InMemoryJobQueue, **changes: object) -> None:
    fields: dict[str, object] = {"kind": KIND, "payload": {}} | changes
    assert await queue.enqueue(NewJob(**fields), None)  # type: ignore[arg-type]


async def job_1(queue: InMemoryJobQueue) -> Job:
    job = await queue.get(1)
    assert job is not None
    return job


def raising(exc: Callable[[], BaseException]) -> tuple[Handler, list[int]]:
    calls: list[int] = []

    async def handler(job: Job) -> None:
        calls.append(job.attempts)
        raise exc()

    return handler, calls


async def test_a_successful_handler_completes_the_job() -> None:
    clock, calls = FakeClock(T0), []
    queue = InMemoryJobQueue(clock)

    async def handler(job: Job) -> None:
        calls.append(job.id)

    await enqueue(queue)

    assert await make_worker(queue, clock, handler).run_one("c1") is True
    assert calls == [1]
    assert (await job_1(queue)).status is JobStatus.DONE
    assert await make_worker(queue, clock, handler).run_one("c1") is False


async def test_transient_errors_back_off_with_increasing_run_after_then_the_job_is_dead() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)

    await assert_transient_errors_back_off_until_dead(queue, clock, queue.get)


async def test_full_jitter_draws_the_delay_below_the_ceiling() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    handler, _ = raising(lambda: TransientError("timeout"))
    await enqueue(queue)

    await make_worker(queue, clock, handler, jitter=lambda: 0.5).run_one("c1")

    assert (await job_1(queue)).run_after == T0 + timedelta(seconds=15)


async def test_retry_after_from_the_error_replaces_the_backoff() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    handler, _ = raising(lambda: TransientError("429", retry_after=timedelta(seconds=90)))
    await enqueue(queue)

    await make_worker(queue, clock, handler).run_one("c1")

    assert (await job_1(queue)).run_after == T0 + timedelta(seconds=90)


async def test_a_permanent_error_fails_the_job_without_retrying() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    handler, calls = raising(lambda: PermanentError("logs expired (410)"))
    await enqueue(queue)

    await make_worker(queue, clock, handler).run_one("c1")
    clock.advance(timedelta(hours=1))

    failed = await job_1(queue)
    assert failed.status is JobStatus.FAILED
    assert failed.last_error == "PermanentError: logs expired (410)"
    assert await make_worker(queue, clock, handler).run_one("c1") is False
    assert calls == [1]


async def test_an_unexpected_exception_is_retried_like_a_transient_one() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    handler, _ = raising(lambda: ValueError("bug"))
    await enqueue(queue)

    await make_worker(queue, clock, handler).run_one("c1")

    retried = await job_1(queue)
    assert retried.status is JobStatus.QUEUED
    assert retried.last_error == "ValueError: bug"


async def test_a_kind_without_a_handler_fails_the_job() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    handler, calls = raising(lambda: AssertionError("not called"))
    await enqueue(queue, kind=JobKind.PURGE)

    await make_worker(queue, clock, handler).run_one("c1")

    failed = await job_1(queue)
    assert failed.status is JobStatus.FAILED
    assert failed.last_error is not None
    assert "no handler" in failed.last_error
    assert calls == []


async def test_a_lost_lease_cancels_the_handler_and_leaves_the_job_to_its_new_owner() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    # Lease of 0.3 s: heartbeats every 0.1 s of real time.
    worker = make_worker(queue, clock, handler, lease=timedelta(seconds=0.3))
    await enqueue(queue)
    running = asyncio.create_task(worker.run_one("c1"))
    await asyncio.wait_for(started.wait(), 2)

    clock.advance(timedelta(seconds=1))  # our lease expires on the fake clock...
    taken = await queue.lease("other", timedelta(minutes=5))  # ...and someone else takes the job
    assert taken is not None

    async with asyncio.timeout(2):
        assert await running is True
    assert cancelled.is_set()
    current = await job_1(queue)
    assert (current.status, current.attempts, current.locked_by) == (JobStatus.RUNNING, 2, "other")


async def test_run_processes_jobs_until_stopped() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    done: list[int] = []
    all_done = asyncio.Event()

    async def handler(job: Job) -> None:
        done.append(job.id)
        if len(done) == 3:
            all_done.set()

    worker = make_worker(queue, clock, handler, concurrency=2)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    for _ in range(3):
        await enqueue(queue)

    await asyncio.wait_for(all_done.wait(), 2)
    stop.set()
    async with asyncio.timeout(2):
        await running

    assert sorted(done) == [1, 2, 3]


async def test_stopping_lets_the_running_job_finish_within_the_grace_period() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await release.wait()

    worker = make_worker(queue, clock, handler, shutdown_grace=timedelta(seconds=5))
    stop = asyncio.Event()
    await enqueue(queue)
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), 2)

    stop.set()
    await asyncio.sleep(0.05)
    assert not running.done()
    release.set()
    async with asyncio.timeout(2):
        await running

    assert (await job_1(queue)).status is JobStatus.DONE


async def test_cancelling_run_also_drains_the_running_job() -> None:
    """Ctrl+C on Windows cancels the main task instead of setting the stop event."""
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await release.wait()

    worker = make_worker(queue, clock, handler, shutdown_grace=timedelta(seconds=5))
    await enqueue(queue)
    running = asyncio.create_task(worker.run(asyncio.Event()))
    await asyncio.wait_for(started.wait(), 2)

    running.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 2)

    assert (await job_1(queue)).status is JobStatus.DONE


async def test_a_job_still_running_after_the_grace_period_is_cancelled_and_left_running() -> None:
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    started = asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.Event().wait()

    worker = make_worker(queue, clock, handler, shutdown_grace=timedelta(seconds=0.1))
    stop = asyncio.Event()
    await enqueue(queue)
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), 2)

    stop.set()
    async with asyncio.timeout(2):
        await running

    assert (await job_1(queue)).status is JobStatus.RUNNING  # the lease will expire


async def test_a_consumer_survives_a_failing_queue() -> None:
    clock = FakeClock(T0)
    done: list[int] = []

    class FlakyQueue(InMemoryJobQueue):
        failures = 1

        async def lease(self, worker_id: str, lease: timedelta) -> Job | None:
            if self.failures:
                self.failures -= 1
                raise TransientError("database unavailable")
            return await super().lease(worker_id, lease)

    handled = asyncio.Event()

    async def handler(job: Job) -> None:
        done.append(job.id)
        handled.set()

    queue = FlakyQueue(clock)
    await enqueue(queue)
    worker = make_worker(queue, clock, handler, poll_interval=timedelta(seconds=0.05))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))

    await asyncio.wait_for(handled.wait(), 2)
    stop.set()
    await running

    assert done == [1]


def test_concurrency_must_be_positive() -> None:
    clock = FakeClock(T0)
    handler, _ = raising(lambda: AssertionError())

    with pytest.raises(ValueError, match="concurrency"):
        make_worker(InMemoryJobQueue(clock), clock, handler, concurrency=0)
