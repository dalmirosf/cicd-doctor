"""Worker scenarios shared by the unit tests (memory) and the integration tests (Postgres)."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Job, JobKind, JobStatus, NewJob
from cicd_doctor.core.ports import JobQueue
from cicd_doctor.worker import Worker


async def assert_transient_errors_back_off_until_dead(
    queue: JobQueue, clock: FakeClock, get: Callable[[int], Awaitable[Job | None]]
) -> None:
    """SPEC §13 P1a: a transient error is retried with increasing ``run_after``, then ``dead``.

    The jitter is pinned to 1.0, so the waits are exactly the backoff ceilings of SPEC §8.
    """
    calls: list[int] = []

    async def handler(job: Job) -> None:
        calls.append(job.attempts)
        raise TransientError("GitHub 502")

    worker = Worker(queue, {JobKind.DIAGNOSE_RUN: handler}, clock, name="t", jitter=lambda: 1.0)
    assert await queue.enqueue(NewJob(kind=JobKind.DIAGNOSE_RUN, payload={}, max_attempts=5), None)

    delays: list[timedelta] = []
    run_afters = []
    job_id = 1  # the queue starts empty (pg_clean also restarts the id sequence)
    for _ in range(4):
        started = clock.now()
        assert await worker.run_one("c1") is True
        queued = await get(job_id)
        assert queued is not None
        assert queued.status is JobStatus.QUEUED
        assert queued.last_error == "TransientError: GitHub 502"
        delays.append(queued.run_after - started)
        run_afters.append(queued.run_after)
        assert await worker.run_one("c1") is False  # not ready before run_after
        clock.advance(queued.run_after - clock.now())
    assert await worker.run_one("c1") is True

    dead = await get(job_id)
    assert dead is not None
    assert delays == [
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=8),
        timedelta(minutes=30),
    ]
    assert run_afters == sorted(set(run_afters))  # strictly increasing
    assert calls == [1, 2, 3, 4, 5]
    assert dead.status is JobStatus.DEAD
    assert dead.attempts == 5
    assert await worker.run_one("c1") is False
