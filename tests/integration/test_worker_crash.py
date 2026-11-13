"""SPEC §13 P1a: a worker killed mid-job → the job is leased again and completes once."""

import asyncio
import subprocess
import sys
from datetime import timedelta

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.core.models import Job, JobKind, JobStatus, NewJob
from cicd_doctor.worker import Worker
from tests.pg import REPO_ROOT

LEASE = timedelta(seconds=2)


def _start_victim(database_url: str) -> subprocess.Popen[str]:
    """A separate process, so the kill is real (asyncio subprocesses need Proactor on Windows)."""
    return subprocess.Popen(
        [sys.executable, "-m", "tests.integration.crash_worker", database_url, "2"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def test_a_worker_killed_mid_job_is_replaced_and_the_job_completes_once(
    pg_clean: str,
) -> None:
    clock = SystemClock()
    async with PostgresJobQueue.connect(pg_clean, clock) as queue:
        assert await queue.enqueue(NewJob(kind=JobKind.DIAGNOSE_RUN, payload={}), None)

        victim = _start_victim(pg_clean)
        assert victim.stdout is not None
        assert victim.stderr is not None
        try:
            async with asyncio.timeout(30):
                line = await asyncio.to_thread(victim.stdout.readline)
            assert line.strip() == "STARTED 1 1", line or victim.stderr.read()
        finally:
            victim.kill()  # a real kill: no cleanup, no lease release
            await asyncio.to_thread(victim.wait, 10)

        orphaned = await queue.get(1)
        assert orphaned is not None
        assert (orphaned.status, orphaned.attempts, orphaned.locked_by) == (
            JobStatus.RUNNING,
            1,
            "victim:0",
        )

        runs: list[Job] = []
        finished = asyncio.Event()

        async def handler(job: Job) -> None:
            runs.append(job)
            finished.set()

        survivor = Worker(
            queue,
            {JobKind.DIAGNOSE_RUN: handler},
            clock,
            concurrency=2,
            lease=LEASE,
            poll_interval=timedelta(seconds=0.2),
            name="survivor",
        )
        stop = asyncio.Event()
        running = asyncio.create_task(survivor.run(stop))
        try:
            await asyncio.wait_for(finished.wait(), 15)
            await asyncio.sleep(LEASE.total_seconds() * 1.5)  # room for a duplicate to show up
        finally:
            stop.set()
            await running

        done = await queue.get(1)
        assert done is not None
        assert [job.attempts for job in runs] == [2]
        assert done.status is JobStatus.DONE
        assert done.attempts == 2
        assert done.locked_by is not None
        assert done.locked_by.startswith("survivor:")
