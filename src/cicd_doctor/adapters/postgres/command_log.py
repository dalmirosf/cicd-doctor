"""Postgres ``CommandLog`` over ``command_invocations`` (D-93).

``claim`` runs in one transaction that first takes ``pg_advisory_xact_lock`` on the pair
``(repository, pull request)``: two workers that claim for the same PR are serialised, so exactly
one of two racing commands wins, while different PRs never wait for each other. Under the lock the
transaction (1) answers ``True`` if this comment already has a row (a retried job or a redelivery),
(2) answers ``False`` if another row of the PR is younger than the window, and (3) inserts. The
time is the one the caller passes (its injected ``Clock``), never ``now()`` of the database.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cicd_doctor.adapters.postgres.engine import PURGE_BATCH, make_engine, transient_db_errors
from cicd_doctor.core.models import PurgeCounts

_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_OWN = text(
    "SELECT 1 FROM command_invocations WHERE repository_id = :repo_id AND comment_id = :comment_id"
)
_RECENT = text(
    """
    SELECT 1 FROM command_invocations
    WHERE repository_id = :repo_id AND pr_number = :pr_number AND created_at > :since
    LIMIT 1
    """
)
_INSERT = text(
    """
    INSERT INTO command_invocations (repository_id, pr_number, comment_id, actor_login, created_at)
    VALUES (:repo_id, :pr_number, :comment_id, :actor_login, :now)
    """
)
_PURGE = text(
    """
    DELETE FROM command_invocations WHERE (repository_id, comment_id) IN (
        SELECT repository_id, comment_id FROM command_invocations
        WHERE created_at < :before
        ORDER BY created_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    """
)
_COUNT = text(
    "SELECT count(*) FROM command_invocations"
    " WHERE (CAST(:repo_id AS bigint) IS NULL OR repository_id = :repo_id)"
)


class PostgresCommandLog:
    """Implements ``CommandLog``. Create it with ``PostgresCommandLog.connect(...)``."""

    def __init__(self, engine: AsyncEngine, *, purge_batch: int = PURGE_BATCH) -> None:
        self._engine = engine
        self._purge_batch = purge_batch

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str, *, pool_size: int = 5) -> AsyncIterator[Self]:
        """``database_url`` is a SQLAlchemy URL (``postgresql+psycopg://...``)."""
        engine = make_engine(database_url, pool_size=pool_size)
        try:
            yield cls(engine)
        finally:
            await engine.dispose()

    async def claim(
        self,
        repo_id: int,
        pr_number: int,
        comment_id: int,
        actor_login: str,
        now: datetime,
        window: timedelta,
    ) -> bool:
        key = {"repo_id": repo_id, "pr_number": pr_number}
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(_LOCK, {"key": f"command:{repo_id}:{pr_number}"})
                own = await conn.execute(_OWN, {"repo_id": repo_id, "comment_id": comment_id})
                if own.first() is not None:
                    return True
                recent = await conn.execute(_RECENT, {**key, "since": now - window})
                if recent.first() is not None:
                    return False
                await conn.execute(
                    _INSERT,
                    {**key, "comment_id": comment_id, "actor_login": actor_login, "now": now},
                )
        return True

    async def purge(self, before: datetime) -> PurgeCounts:
        total = 0
        while True:
            with transient_db_errors():
                async with self._engine.begin() as conn:
                    result = await conn.execute(
                        _PURGE, {"before": before, "batch": self._purge_batch}
                    )
            total += result.rowcount
            if result.rowcount < self._purge_batch:
                return PurgeCounts(deleted={"command_invocations": total})

    # --- Inspection (tests) -----------------------------------------------------------------

    async def count(self, repo_id: int | None = None) -> int:
        async with self._engine.connect() as conn:
            return int((await conn.execute(_COUNT, {"repo_id": repo_id})).scalar_one())
