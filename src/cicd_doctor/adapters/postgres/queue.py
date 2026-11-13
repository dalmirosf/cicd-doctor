"""Postgres ``JobQueue`` (SPEC §6, D-05, D-47, D-48).

SQLAlchemy 2 Core (async) on psycopg 3 with explicit SQL, so the ``SKIP LOCKED`` statements
read exactly like the spec (D-28). Times come from the injected ``Clock`` and are passed as
parameters instead of SQL ``now()`` (D-48). New jobs are announced with ``pg_notify`` in the
enqueue transaction; a dedicated autocommit psycopg connection listens on ``jobs_new``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timedelta
from typing import Any, Self

import psycopg
from sqlalchemy import RowMapping, make_url, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from cicd_doctor.adapters.wakeup import Wakeup
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Delivery, Job, JobKind, JobStatus, NewJob
from cicd_doctor.core.ports import Clock
from cicd_doctor.core.retry import truncate_error

logger = logging.getLogger(__name__)

CHANNEL = "jobs_new"
CONNECT_TIMEOUT_S = 10
EXHAUSTED_ERROR = "no attempts left: the last attempt did not finish before its lease expired"
_LISTEN_RETRY_MAX_S = 30.0
_MAX_SKIPPED_ROWS = 10  # unknown kinds failed in one lease call before giving up for this poll

_JOB_COLUMNS = (
    "id, kind, payload, dedupe_key, status, attempts, max_attempts, run_after, locked_until, "
    "locked_by, last_error"
)

_INSERT_DELIVERY = text(
    """
    INSERT INTO webhook_deliveries (delivery_id, event, action, installation_id, received_at)
    VALUES (:delivery_id, :event, :action, :installation_id, :now)
    ON CONFLICT DO NOTHING
    RETURNING delivery_id
    """
)
_INSERT_JOB = text(
    """
    INSERT INTO jobs (kind, payload, dedupe_key, delivery_id, max_attempts, run_after,
                      created_at, updated_at)
    VALUES (:kind, CAST(:payload AS jsonb), :dedupe_key, :delivery_id, :max_attempts, :run_after,
            :now, :now)
    ON CONFLICT (dedupe_key) DO NOTHING
    RETURNING id
    """
)
_NOTIFY = text(f"SELECT pg_notify('{CHANNEL}', '')")

# Jobs whose last allowed attempt never finished (crashed worker) are buried, not run again.
_SWEEP_EXHAUSTED = text(
    """
    UPDATE jobs
    SET status = 'dead', locked_until = NULL, last_error = :error, updated_at = :now
    WHERE id IN (
        SELECT id FROM jobs
        WHERE status IN ('queued', 'running') AND attempts >= max_attempts
          AND (locked_until IS NULL OR locked_until < :now)
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, kind
    """
)
_LEASE = text(
    f"""
    UPDATE jobs
    SET status = 'running', attempts = attempts + 1, locked_by = :worker_id,
        locked_until = :locked_until, updated_at = :now
    WHERE id = (
        SELECT id FROM jobs
        WHERE status IN ('queued', 'running') AND run_after <= :now
          AND (locked_until IS NULL OR locked_until < :now)
          AND attempts < max_attempts
        ORDER BY run_after, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING {_JOB_COLUMNS}
    """
)
_FAIL_UNREADABLE = text(
    """
    UPDATE jobs SET status = 'failed', locked_until = NULL, last_error = :error, updated_at = :now
    WHERE id = :id
    """
)
# Every transition of a leased job is fenced by (id, attempts): a stale lease changes nothing.
_FENCE = "WHERE id = :id AND attempts = :attempts AND status = 'running'"
_HEARTBEAT = text(f"UPDATE jobs SET locked_until = :locked_until, updated_at = :now {_FENCE}")
_COMPLETE = text(
    f"UPDATE jobs SET status = 'done', locked_until = NULL, updated_at = :now {_FENCE}"
)
_RETRY = text(
    f"""
    UPDATE jobs
    SET status = 'queued', run_after = :run_after, last_error = :error, locked_until = NULL,
        locked_by = NULL, updated_at = :now
    {_FENCE}
    """
)
_FAIL = text(
    f"""
    UPDATE jobs
    SET status = CAST(:status AS job_status), last_error = :error, locked_until = NULL,
        updated_at = :now
    {_FENCE}
    """
)
_GET = text(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = :id")

# Connection-level problems are worth retrying (D-23); anything else is a bug and propagates.
_TRANSIENT_DB_ERRORS = (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)


class PostgresJobQueue:
    """Implements ``JobQueue``. Create it with ``PostgresJobQueue.connect(...)``."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock
        self._wakeup = Wakeup()
        self._listening = asyncio.Event()

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        database_url: str,
        clock: Clock,
        *,
        listen: bool = True,
        pool_size: int = 5,
    ) -> AsyncIterator[Self]:
        """Open the engine and, with ``listen=True``, the ``LISTEN jobs_new`` connection.

        ``database_url`` is a SQLAlchemy URL (``postgresql+psycopg://...``).
        """
        engine = create_async_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=pool_size,
            pool_pre_ping=True,
            connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
        )
        queue = cls(engine, clock)
        listener: asyncio.Task[None] | None = None
        try:
            if listen:
                listener = asyncio.create_task(queue._listen(_libpq_url(database_url)))
                # Give the listener a moment, so a job enqueued right after connect() wakes us.
                # If the database is down the worker still starts and falls back to polling.
                with suppress(TimeoutError):
                    async with asyncio.timeout(CONNECT_TIMEOUT_S):
                        await queue._listening.wait()
            yield queue
        finally:
            if listener is not None:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener
            await engine.dispose()

    # --- JobQueue ---------------------------------------------------------------------------

    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        now = self._clock.now()
        with _transient_db_errors():
            async with self._engine.begin() as conn:
                if delivery is not None:
                    inserted = await conn.execute(
                        _INSERT_DELIVERY,
                        {
                            "delivery_id": delivery.delivery_id,
                            "event": delivery.event,
                            "action": delivery.action,
                            "installation_id": delivery.installation_id,
                            "now": now,
                        },
                    )
                    if inserted.first() is None:
                        return False
                created = await conn.execute(
                    _INSERT_JOB,
                    {
                        "kind": job.kind.value,
                        "payload": json.dumps(job.payload),
                        "dedupe_key": job.dedupe_key,
                        "delivery_id": delivery.delivery_id if delivery else None,
                        "max_attempts": job.max_attempts,
                        "run_after": job.run_after or now,
                        "now": now,
                    },
                )
                if created.first() is None:
                    return False
                await conn.execute(_NOTIFY)  # delivered when the transaction commits
        return True

    async def lease(self, worker_id: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        with _transient_db_errors():
            async with self._engine.begin() as conn:
                await self._sweep_exhausted(conn, now)
                for _ in range(_MAX_SKIPPED_ROWS):
                    row = (
                        await conn.execute(
                            _LEASE,
                            {"worker_id": worker_id, "locked_until": now + lease, "now": now},
                        )
                    ).first()
                    if row is None:
                        return None
                    job = await self._job_or_fail(conn, row._mapping, now)
                    if job is not None:
                        return job
        return None

    async def heartbeat(self, job: Job, lease: timedelta) -> bool:
        now = self._clock.now()
        return await self._fenced(_HEARTBEAT, job, {"locked_until": now + lease, "now": now})

    async def complete(self, job: Job) -> bool:
        return await self._fenced(_COMPLETE, job, {"now": self._clock.now()})

    async def retry(self, job: Job, run_after: datetime, error: str) -> bool:
        params = {"run_after": run_after, "error": truncate_error(error), "now": self._clock.now()}
        return await self._fenced(_RETRY, job, params)

    async def fail(self, job: Job, error: str, dead: bool) -> bool:
        status = JobStatus.DEAD if dead else JobStatus.FAILED
        params = {"status": status.value, "error": truncate_error(error), "now": self._clock.now()}
        return await self._fenced(_FAIL, job, params)

    async def wait_for_jobs(self, max_wait: timedelta) -> bool:
        return await self._wakeup.wait(max_wait)

    # --- Inspection (tests, operations) -----------------------------------------------------

    async def get(self, job_id: int) -> Job | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(_GET, {"id": job_id})).first()
        return None if row is None else _to_job(row._mapping)

    # --- Internals --------------------------------------------------------------------------

    async def _fenced(self, statement: Any, job: Job, params: dict[str, Any]) -> bool:
        with _transient_db_errors():
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    statement, {"id": job.id, "attempts": job.attempts, **params}
                )
        return result.rowcount == 1

    async def _sweep_exhausted(self, conn: AsyncConnection, now: datetime) -> None:
        buried = await conn.execute(_SWEEP_EXHAUSTED, {"error": EXHAUSTED_ERROR, "now": now})
        for job_id, kind in buried:
            logger.error("job %s (%s) is dead: %s", job_id, kind, EXHAUSTED_ERROR)

    async def _job_or_fail(
        self, conn: AsyncConnection, row: RowMapping, now: datetime
    ) -> Job | None:
        """Parse a leased row; a row that is not a valid job (unknown kind) becomes ``failed``."""
        try:
            return _to_job(row)
        except ValueError as exc:
            error = f"unreadable job row: {exc}"
            await conn.execute(
                _FAIL_UNREADABLE, {"id": row["id"], "error": truncate_error(error), "now": now}
            )
            logger.error("job %s failed: %s", row["id"], error)
            return None

    async def _listen(self, conninfo: str) -> None:
        delay = 1.0
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    conninfo, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S
                )
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self._listening.set()
                    delay = 1.0
                    self._wakeup.notify_all()  # jobs may have arrived while we were away
                    async for _ in conn.notifies():
                        self._wakeup.notify_all()
            except Exception as exc:  # never let the listener die: wake-ups would stop for good
                logger.warning(
                    "LISTEN %s lost (%s); polling until it reconnects", CHANNEL, type(exc).__name__
                )
            self._listening.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, _LISTEN_RETRY_MAX_S)


def _to_job(row: RowMapping) -> Job:
    return Job(
        id=row["id"],
        kind=JobKind(row["kind"]),
        payload=row["payload"],
        dedupe_key=row["dedupe_key"],
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_after=row["run_after"],
        locked_until=row["locked_until"],
        locked_by=row["locked_by"],
        last_error=row["last_error"],
    )


def _libpq_url(database_url: str) -> str:
    """``postgresql+psycopg://...`` (SQLAlchemy) -> ``postgresql://...`` (libpq)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def _transient_db_errors() -> Iterator[None]:
    try:
        yield
    except _TRANSIENT_DB_ERRORS as exc:
        cause = getattr(exc, "orig", None) or exc
        raise TransientError(f"database unavailable: {type(cause).__name__}") from exc
