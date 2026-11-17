"""The daily scheduler (D-85): one ``purge`` job per UTC day, however many workers run."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.core.models import Delivery, JobKind, NewJob
from cicd_doctor.worker.scheduler import Scheduler, purge_dedupe_key

DAY = timedelta(days=1)
TWO_AM = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)


async def purge_jobs(queue: InMemoryJobQueue) -> list[int]:
    return [n for n in range(1, 50) if (job := await queue.get(n)) and job.kind is JobKind.PURGE]


async def test_three_schedulers_on_the_same_queue_create_one_job_per_day() -> None:
    clock = FakeClock(TWO_AM)
    queue = InMemoryJobQueue(clock)
    schedulers = [Scheduler(queue, clock) for _ in range(3)]

    created = [await s.tick() for s in schedulers] + [await s.tick() for s in schedulers]

    assert created.count(True) == 1
    assert len(await purge_jobs(queue)) == 1


async def test_at_two_in_the_morning_the_job_is_ready_at_three() -> None:
    clock = FakeClock(TWO_AM)
    queue = InMemoryJobQueue(clock)

    await Scheduler(queue, clock, hour_utc=3).tick()

    job = await queue.get(1)
    assert job is not None
    assert job.run_after == datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
    assert job.dedupe_key == "purge:2026-11-17"
    assert await queue.lease("w", timedelta(seconds=300)) is None  # not before 03:00
    clock.advance(timedelta(hours=1))
    leased = await queue.lease("w", timedelta(seconds=300))
    assert leased is not None
    assert leased.kind is JobKind.PURGE


async def test_the_next_day_creates_another_job() -> None:
    clock = FakeClock(TWO_AM)
    queue = InMemoryJobQueue(clock)
    scheduler = Scheduler(queue, clock)
    await scheduler.tick()

    clock.advance(DAY)

    assert await scheduler.tick() is True
    assert len(await purge_jobs(queue)) == 2
    second = await queue.get(2)
    assert second is not None
    assert second.dedupe_key == purge_dedupe_key(datetime(2026, 11, 18, tzinfo=UTC).date())


async def test_a_clock_in_another_zone_still_counts_days_in_utc() -> None:
    clock = FakeClock(datetime(2026, 11, 17, 23, 30, tzinfo=UTC))
    queue = InMemoryJobQueue(clock)
    scheduler = Scheduler(queue, clock)

    await scheduler.tick()
    clock.advance(timedelta(minutes=45))  # 00:15 UTC of the next day

    assert await scheduler.tick() is True


class BrokenQueue(InMemoryJobQueue):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.failures = 1

    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("database is down")
        return await super().enqueue(job, delivery)


async def test_a_queue_that_fails_is_logged_and_the_next_tick_works(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(TWO_AM)
    queue = BrokenQueue(clock)
    scheduler = Scheduler(queue, clock)

    assert await scheduler.tick() is False
    assert "could not enqueue" in caplog.text
    assert await scheduler.tick() is True


async def test_run_keeps_ticking_until_stopped_even_when_the_queue_fails() -> None:
    clock = FakeClock(TWO_AM)
    queue = BrokenQueue(clock)
    scheduler = Scheduler(queue, clock, interval=timedelta(milliseconds=10))
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.2)  # a few 10 ms ticks: the first one fails, a later one succeeds
    stop.set()
    await asyncio.wait_for(task, 1)

    assert len(await purge_jobs(queue)) == 1


def test_the_hour_must_be_a_real_hour() -> None:
    with pytest.raises(ValueError, match="hour_utc"):
        Scheduler(InMemoryJobQueue(), FakeClock(), hour_utc=24)


async def test_no_log_noise_when_the_job_already_exists(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock(TWO_AM)
    queue = InMemoryJobQueue(clock)
    scheduler = Scheduler(queue, clock)
    await scheduler.tick()
    caplog.clear()
    caplog.set_level(logging.INFO)

    await scheduler.tick()

    assert caplog.records == []
