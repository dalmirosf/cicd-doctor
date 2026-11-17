"""Composition root (D-39): builds the API and the worker from the settings.

``sync_installation`` and ``diagnose_run`` have real handlers; every other job kind gets a
placeholder that logs the job and completes it. The worker needs the GitHub App settings
(``GITHUB_APP_ID``, ``GITHUB_PRIVATE_KEY``) and refuses to start without them; the LLM client
exists only when both ``OPENAI_API_KEY`` and ``OPENAI_MODEL`` are set (otherwise the pipeline
publishes the rules diagnosis with the reason ``disabled``).
"""

import asyncio
import logging
import signal
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from pydantic import SecretStr

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.embeddings.fastembed_embedder import FastEmbedEmbedder
from cicd_doctor.adapters.github.auth import GitHubAppAuth, load_private_key
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.adapters.github.logs import GitHubLogSource
from cicd_doctor.adapters.github.pulls import GitHubPullRequests
from cicd_doctor.adapters.github.reporter import GitHubReporter
from cicd_doctor.adapters.llm.openai_client import OpenAILLMClient, make_openai_client
from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.ledger import PostgresUsageLedger
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.core.ports import Clock, Embedder, InstallationStore, JobQueue, LLMClient
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig, DiagnoseFailedRun
from cicd_doctor.core.usecases.purge import PurgeConfig
from cicd_doctor.worker import Handler, Worker
from cicd_doctor.worker.handlers import (
    diagnose_run_handler,
    purge_handler,
    sync_installation_handler,
)
from cicd_doctor.worker.scheduler import Scheduler

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


# At INFO these print the URL of every request, and the URL of a log blob carries its signature
# in the query (D-67). Their children (``httpcore2.http11`` and so on) inherit the level.
HTTP_LOGGERS = ("httpx2", "httpcore2")


def configure_logging() -> None:
    """Plain-text logs on stderr until structlog arrives (D-32, D-49). Idempotent."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


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


class ConfigurationError(Exception):
    """A setting the worker needs is missing. The message names variables, never values."""


async def placeholder_handler(job: Job) -> None:
    # Never log the payload: it carries repository data.
    logger.warning("job %s (%s): no use case wired yet (placeholder)", job.id, job.kind)


def require_github_app(settings: Settings) -> tuple[int, SecretStr]:
    """The app id and the private key, or a ``ConfigurationError`` that names what is missing."""
    missing = [
        name
        for name, value in (
            ("GITHUB_APP_ID", settings.github_app_id),
            ("GITHUB_PRIVATE_KEY", settings.github_private_key),
        )
        if value is None
    ]
    if missing or settings.github_app_id is None or settings.github_private_key is None:
        raise ConfigurationError(
            f"the worker cannot start: {' and '.join(missing)} must be set (see .env.example)"
        )
    return settings.github_app_id, settings.github_private_key


def make_llm_client(settings: Settings) -> LLMClient | None:
    """The OpenAI client, or ``None`` (the pipeline then publishes the rules diagnosis) unless
    both ``OPENAI_API_KEY`` and ``OPENAI_MODEL`` are set. Logs which one is missing, never a
    value."""
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", settings.openai_api_key),
            ("OPENAI_MODEL", settings.openai_model),
        )
        if not value
    ]
    if missing or settings.openai_api_key is None or settings.openai_model is None:
        logger.warning(
            "LLM disabled: %s not set, diagnoses use the rules only", " and ".join(missing)
        )
        return None
    return OpenAILLMClient(
        make_openai_client(
            settings.openai_api_key.get_secret_value(), timeout_s=settings.llm_timeout_s
        ),
        model=settings.openai_model,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_s=settings.llm_timeout_s,
    )


def cost_model(settings: Settings) -> CostModel:
    """Prices from ``LLM_PRICE_*``; unset prices mean 0, so only the call limit applies."""
    if settings.llm_price_input_per_mtok is None or settings.llm_price_output_per_mtok is None:
        logger.warning(
            "LLM_PRICE_* not set: LLM calls cost 0 and the daily budget cannot stop them"
        )
    return CostModel(
        input_per_mtok=settings.llm_price_input_per_mtok or Decimal(0),
        output_per_mtok=settings.llm_price_output_per_mtok or Decimal(0),
    )


def make_embedder(settings: Settings) -> FastEmbedEmbedder | None:
    """The fastembed embedder, or ``None`` when ``EMBEDDINGS_ENABLED=false`` (D-78). The model
    is not loaded here: ``start_warming`` does it in the background."""
    if not settings.embeddings_enabled:
        logger.warning("EMBEDDINGS_ENABLED=false: failures are stored without an embedding")
        return None
    return FastEmbedEmbedder(settings.embedding_cache_dir, threads=settings.embedding_threads)


def start_warming(embedder: FastEmbedEmbedder | None) -> threading.Thread | None:
    """Load the model in a background thread while the worker consumes jobs (D-78).

    Nothing waits for it: until it is loaded ``embed`` fails fast and the diagnosis goes on
    without a vector. A daemon thread, so a download in progress never delays a shutdown. A load
    that fails is logged as an error and retried by ``embed`` every 5 minutes at most.
    """
    if embedder is None:
        return None
    thread = threading.Thread(target=embedder.warm, name="embedding-warm", daemon=True)
    thread.start()
    return thread


def diagnose_config(settings: Settings) -> DiagnoseConfig:
    return DiagnoseConfig(
        llm_enabled=settings.llm_enabled,
        max_jobs=settings.log_max_jobs,
        max_bytes=settings.log_max_bytes,
        excerpt_max_tokens=settings.excerpt_max_tokens,
    )


def purge_config(settings: Settings) -> PurgeConfig:
    return PurgeConfig(
        deliveries_days=settings.retention_deliveries_days,
        failures_days=settings.retention_failures_days,
        jobs_days=settings.retention_jobs_days,
        stale_after_days=settings.stale_after_days,
        uninstall_grace_days=settings.uninstall_grace_days,
    )


def handlers(
    installations: InstallationStore, diagnose_run: Handler, purge: Handler
) -> dict[JobKind, Handler]:
    registry: dict[JobKind, Handler] = dict.fromkeys(JobKind, placeholder_handler)
    registry[JobKind.SYNC_INSTALLATION] = sync_installation_handler(installations)
    registry[JobKind.DIAGNOSE_RUN] = diagnose_run
    registry[JobKind.PURGE] = purge
    return registry


def build_worker(
    settings: Settings, queue: JobQueue, registry: dict[JobKind, Handler], clock: Clock
) -> Worker:
    return Worker(
        queue,
        registry,
        clock,
        concurrency=settings.worker_concurrency,
        lease=timedelta(seconds=settings.job_lease_s),
    )


@asynccontextmanager
async def diagnose_run_use_case(
    settings: Settings,
    clock: Clock,
    installations: InstallationStore,
    embedder: Embedder | None = None,
) -> AsyncIterator[DiagnoseFailedRun]:
    """The real graph of the diagnosis flow; its connections close when the block ends."""
    app_id, key = require_github_app(settings)
    private_key = load_private_key(key)  # ValueError names the variable, never the key
    url = settings.database_url.get_secret_value()
    consumers = settings.worker_concurrency
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(GitHubHttp(clock))
        auth = GitHubAppAuth(http, app_id=app_id, private_key=private_key, clock=clock)
        logs = GitHubLogSource(http, auth)
        stack.push_async_callback(logs.aclose)
        # A publication lock holds one pooled connection for the whole GitHub call and the
        # ``save_publication`` inside it needs a second one: two per job in flight (one per
        # consumer), or the pool could deadlock.
        store = await stack.enter_async_context(
            PostgresFailureStore.connect(
                url,
                clock,
                pool_size=2 * consumers + 2,
                use_hnsw=settings.similar_use_hnsw,
            )
        )
        ledger = await stack.enter_async_context(
            PostgresUsageLedger.connect(
                url,
                clock,
                calls_per_installation=settings.llm_daily_calls_per_installation,
                daily_budget_usd=settings.llm_daily_budget_usd,
                pool_size=consumers + 2,
            )
        )
        yield DiagnoseFailedRun(
            logs=logs,
            pulls=GitHubPullRequests(http, auth),
            reporter=GitHubReporter(http, auth, clock),
            store=store,
            installations=installations,
            counter=TiktokenCounter(),
            diagnose=DiagnoseJob(
                llm=make_llm_client(settings),
                ledger=ledger,
                cost=cost_model(settings),
                max_output_tokens=settings.llm_max_output_tokens,
            ),
            clock=clock,
            embedder=embedder,
            config=diagnose_config(settings),
        )


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    require_github_app(settings)  # before any connection opens
    clock = SystemClock()
    url = settings.database_url.get_secret_value()
    embedder = make_embedder(settings)
    start_warming(embedder)  # the consumers do not wait for the model (D-78)
    async with (
        PostgresJobQueue.connect(
            url,
            clock,
            pool_size=settings.worker_concurrency + 2,  # consumers + heartbeats
        ) as queue,
        PostgresInstallationStore.connect(
            url, pool_size=settings.worker_concurrency + 2
        ) as installations,
        PostgresFailureStore.connect(url, clock, pool_size=2) as purge_store,
        diagnose_run_use_case(settings, clock, installations, embedder) as use_case,
    ):
        purge = purge_handler(queue, purge_store, installations, clock, purge_config(settings))
        registry = handlers(installations, diagnose_run_handler(use_case), purge)
        worker = build_worker(settings, queue, registry, clock)
        if not settings.purge_enabled:
            logger.warning("PURGE_ENABLED=false: the daily purge is not scheduled")
            await worker.run(stop)
            return
        scheduler = Scheduler(queue, clock, hour_utc=settings.purge_hour_utc)
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run(stop))
            group.create_task(scheduler.run(stop))


def serve_worker() -> None:
    """Entry point of ``cicd-doctor worker``. Stops cleanly on SIGTERM and Ctrl+C."""
    configure_logging()
    settings = Settings()
    try:
        _, key = require_github_app(settings)
        load_private_key(key)
    except (ConfigurationError, ValueError) as exc:
        sys.exit(f"cicd-doctor worker: {exc}")  # names the variable, never its value
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
