"""SPEC §13 P1a: a worker that dies mid-job, and exactly one check run is created for the job.

In process: worker A's handler is cancelled while it is blocked downloading the log blob, which
happens after the check run was created and its id stored. The clock passes the lease, worker B
leases the same job and finishes it. ``FakeGitHub`` must have seen exactly one check run ``POST``.
(The kill of a real process is covered for the queue by ``test_worker_crash.py``; the fake GitHub
does not cross processes.)
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timedelta

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.models import JobStatus
from tests.diagnose_world import diagnose_world, job_id_for, log_bytes
from tests.pg import scalar

CHECKS = "/repos/{owner}/{repo}/check-runs"
CHECK = "/repos/{owner}/{repo}/check-runs/{check_run_id}"
COMMENTS = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
BLOB = "/actions-results/{blob_id}/workflow-job-run-{run_uuid}/logs/job/job-logs.txt"
LEASE = timedelta(minutes=5)


async def test_a_worker_cancelled_after_the_check_post_leaves_exactly_one_check_run(
    pg_clean: str,
) -> None:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    reached = asyncio.Event()
    downloads = 0

    async def blob() -> AsyncIterator[bytes]:
        nonlocal downloads
        downloads += 1
        if downloads == 1:  # worker A: stuck here, as if the process died
            reached.set()
            await asyncio.Event().wait()
        yield log_bytes()

    async with (
        PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue,
        PostgresFailureStore.connect(pg_clean, clock) as store,
        PostgresUsageLedger.connect(pg_clean, clock) as ledger,
        PostgresInstallationStore.connect(pg_clean) as installations,
        diagnose_world(
            clock=clock,
            queue=queue,
            store=store,
            ledger=ledger,
            installations=installations,
            lease=LEASE,
        ) as world,
    ):
        job_id = world.add_run()
        world.logs.set_log(job_id, stream=blob)
        await world.enqueue()

        worker_a = asyncio.create_task(world.worker.run_one("worker-a"))
        async with asyncio.timeout(30):
            await reached.wait()
        assert len(world.requests("POST", CHECKS)) == 1  # the check run exists, in progress
        worker_a.cancel()
        with suppress(asyncio.CancelledError):
            await worker_a

        orphaned = await queue.get(1)
        assert orphaned is not None
        assert (orphaned.status, orphaned.attempts) == (JobStatus.RUNNING, 1)
        assert not await world.worker.run_one("worker-b")  # the lease of A is still valid

        clock.advance(LEASE + timedelta(seconds=1))
        assert await world.worker.run_one("worker-b")

        done = await queue.get(1)
        assert done is not None
        assert (done.status, done.attempts, done.locked_by) == (JobStatus.DONE, 2, "worker-b")
        assert len(world.requests("POST", CHECKS)) == 1  # B reopened the one A created
        assert len(world.writes.check_runs) == 1
        [check] = world.writes.check_runs.values()
        assert check["status"] == "completed"
        assert check["conclusion"] == "neutral"
        assert len(world.requests("POST", COMMENTS)) == 1
        assert len(world.writes.comments) == 1
        assert len(world.llm.calls) == 1  # the LLM was called once, by B

    assert scalar(pg_clean, "SELECT count(*) FROM jobs WHERE status = 'done'") == 1
    assert scalar(pg_clean, "SELECT count(*) FROM check_publications") == 1
    assert scalar(pg_clean, "SELECT count(*) FROM failures") == 1
    assert job_id == job_id_for(5000, 1)
