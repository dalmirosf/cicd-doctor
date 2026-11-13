"""Composition root (D-39): builds the API and the worker from the settings.

Only ``sync_installation`` has a real handler so far; every other job kind gets a placeholder
that logs the job and completes it. Each later session replaces the placeholder of its kind with
the real use case.
"""

import asyncio
import logging
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.core.ports import Clock, InstallationStore, JobQueue
from cicd_doctor.worker import Handler, Worker
from cicd_doctor.worker.handlers import sync_installation_handler

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Plain-text logs on stderr until structlog arrives (D-32, D-49). Idempotent."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


# --- API ----------------------------------------------------------------------------------


def webhook_secrets(settings: Settings) -> tuple[bytes, ...]:
    """The current webhook secret, then the previous one while rotating (FR-01)."""
    configured = (settings.github_webhook_secret, settings.github_webhook_secret_previous)
    return tuple(secret.get_secret_value().encode("utf-8") for secret in configured if secret)


@asynccontextmanager
async def api_dependencies() -> AsyncIterator[ApiDependencies]:
    settings = Settings()
    clock = SystemClock()
    secrets = webhook_secrets(settings)
    if not secrets:
        logger.error("GITHUB_WEBHOOK_SECRET is not set: POST /webhooks/github will answer 503")
    async with PostgresJobQueue.connect(
        settings.database_url.get_secret_value(), clock, listen=False
    ) as queue:
        yield ApiDependencies(
            queue=queue,
            webhook_secrets=secrets,
            ready=queue.ping,
            clock=clock,
            max_attempts=settings.job_max_attempts,
        )


def create_api() -> FastAPI:
    """uvicorn factory for ``cicd-doctor api`` (D-55)."""
    configure_logging()
    return create_app(api_dependencies)


# --- Worker -------------------------------------------------------------------------------


async def placeholder_handler(job: Job) -> None:
    # Never log the payload: it carries repository data.
    logger.warning("job %s (%s): no use case wired yet (placeholder)", job.id, job.kind)


def handlers(installations: InstallationStore) -> dict[JobKind, Handler]:
    registry: dict[JobKind, Handler] = dict.fromkeys(JobKind, placeholder_handler)
    registry[JobKind.SYNC_INSTALLATION] = sync_installation_handler(installations)
    return registry


def build_worker(
    settings: Settings, queue: JobQueue, installations: InstallationStore, clock: Clock
) -> Worker:
    return Worker(
        queue,
        handlers(installations),
        clock,
        concurrency=settings.worker_concurrency,
        lease=timedelta(seconds=settings.job_lease_s),
    )


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    clock = SystemClock()
    url = settings.database_url.get_secret_value()
    async with (
        PostgresJobQueue.connect(
            url,
            clock,
            pool_size=settings.worker_concurrency + 2,  # consumers + heartbeats
        ) as queue,
        PostgresInstallationStore.connect(url) as installations,
    ):
        await build_worker(settings, queue, installations, clock).run(stop)


def serve_worker() -> None:
    """Entry point of ``cicd-doctor worker``. Stops cleanly on SIGTERM and Ctrl+C."""
    configure_logging()
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
