"""Composition root (D-39): builds the worker from the settings and runs it.

P1a has no use cases yet, so every job kind gets a placeholder handler that logs the job and
completes it. Nothing enqueues jobs until the webhook arrives (P1c); each later session replaces
the placeholder of its kind with the real use case.
"""

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from datetime import timedelta

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.core.ports import Clock, JobQueue
from cicd_doctor.worker import Handler, Worker

logger = logging.getLogger(__name__)


async def placeholder_handler(job: Job) -> None:
    # Never log the payload: from P1 it carries repository data.
    logger.warning("job %s (%s): no use case wired yet (P1a placeholder)", job.id, job.kind)


def handlers() -> dict[JobKind, Handler]:
    return dict.fromkeys(JobKind, placeholder_handler)


def build_worker(settings: Settings, queue: JobQueue, clock: Clock) -> Worker:
    return Worker(
        queue,
        handlers(),
        clock,
        concurrency=settings.worker_concurrency,
        lease=timedelta(seconds=settings.job_lease_s),
    )


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    clock = SystemClock()
    async with PostgresJobQueue.connect(
        settings.database_url.get_secret_value(),
        clock,
        pool_size=settings.worker_concurrency + 2,  # consumers + heartbeats in flight
    ) as queue:
        await build_worker(settings, queue, clock).run(stop)


def serve_worker() -> None:
    """Entry point of ``cicd-doctor worker``. Stops cleanly on SIGTERM and Ctrl+C."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    # Ctrl+C: asyncio.run cancels the worker, which drains its consumers before it returns.
    with suppress(KeyboardInterrupt):
        asyncio.run(_main(settings), loop_factory=event_loop_factory())


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    if sys.platform != "win32":
        # ECS stops tasks with SIGTERM. On Windows, Ctrl+C reaches asyncio.run, which cancels
        # the worker; Worker.run drains the consumers on cancellation too.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    await run_worker(settings, stop)


def event_loop_factory() -> type[asyncio.AbstractEventLoop]:
    """psycopg's async mode cannot run on Windows' default Proactor loop (D-52)."""
    return asyncio.SelectorEventLoop
