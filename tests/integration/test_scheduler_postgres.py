"""The scheduler on the real queue (D-49, D-85): two workers, one purge job per UTC day."""

import asyncio
from datetime import UTC, datetime, timedelta

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.worker.scheduler import Scheduler
from tests.pg import scalar

TWO_AM = datetime(2026, 11, 17, 2, 0, tzinfo=UTC)


def purge_jobs(url: str) -> int:
    return int(str(scalar(url, "SELECT count(*) FROM jobs WHERE kind = 'purge'")))


async def test_two_engines_create_one_job_per_day(pg_clean: str) -> None:
    clock = FakeClock(TWO_AM)
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as first,
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as second,
    ):
        schedulers = [Scheduler(first, clock), Scheduler(second, clock)] * 3

        results = await asyncio.gather(*(s.tick() for s in schedulers))

        assert results.count(True) == 1
        assert purge_jobs(pg_clean) == 1
        run_after = scalar(pg_clean, "SELECT run_after FROM jobs WHERE kind = 'purge'")
        assert run_after == datetime(2026, 11, 17, 3, 0, tzinfo=UTC)

        clock.advance(timedelta(days=1))
        results = await asyncio.gather(*(s.tick() for s in schedulers))

        assert results.count(True) == 1
        assert purge_jobs(pg_clean) == 2


async def test_only_one_worker_leases_the_job(pg_clean: str) -> None:
    clock = FakeClock(TWO_AM)
    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as first,
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as second,
    ):
        await Scheduler(first, clock).tick()
        await Scheduler(second, clock).tick()
        clock.advance(timedelta(hours=1))

        leased = await asyncio.gather(
            first.lease("a", timedelta(minutes=5)), second.lease("b", timedelta(minutes=5))
        )

        assert sum(job is not None for job in leased) == 1
