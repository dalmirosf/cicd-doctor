"""A worker that takes one job and then hangs, so a test can kill it mid-job.

Run by tests/integration/test_worker_crash.py as:
    python -m tests.integration.crash_worker <database_url> <lease_seconds>

It prints ``STARTED <job_id> <attempt>`` when its handler starts, then waits forever while the
worker keeps heartbeating, until the test kills the process.
"""

import asyncio
import sys
from datetime import timedelta

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.worker import Worker


async def _hang(job: Job) -> None:
    print(f"STARTED {job.id} {job.attempts}", flush=True)
    await asyncio.Event().wait()


async def main(database_url: str, lease_s: float) -> None:
    clock = SystemClock()
    async with PostgresJobQueue.connect(database_url, clock) as queue:
        worker = Worker(
            queue,
            {JobKind.DIAGNOSE_RUN: _hang},
            clock,
            concurrency=1,
            lease=timedelta(seconds=lease_s),
            poll_interval=timedelta(seconds=0.2),
            name="victim",
        )
        await worker.run(asyncio.Event())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], float(sys.argv[2])), loop_factory=asyncio.SelectorEventLoop)
