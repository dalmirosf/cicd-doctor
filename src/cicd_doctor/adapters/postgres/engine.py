"""Shared plumbing for the Postgres adapters: engines, URLs, error mapping, readiness ping."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import exc as sa_exc
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cicd_doctor.core.errors import TransientError

# Fail instead of hanging when the host is unreachable (D-52).
CONNECT_TIMEOUT_S = 10

# Connection-level problems are worth retrying (D-23); anything else is a bug and propagates.
_TRANSIENT_DB_ERRORS = (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)


def make_engine(database_url: str, *, pool_size: int = 5) -> AsyncEngine:
    """An async engine for a SQLAlchemy URL (``postgresql+psycopg://...``)."""
    return create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


def libpq_url(database_url: str) -> str:
    """``postgresql+psycopg://...`` (SQLAlchemy) -> ``postgresql://...`` (libpq)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def transient_db_errors() -> Iterator[None]:
    """Re-raise connection-level database errors as ``TransientError``."""
    try:
        yield
    except _TRANSIENT_DB_ERRORS as exc:
        cause = getattr(exc, "orig", None) or exc
        raise TransientError(f"database unavailable: {type(cause).__name__}") from exc


async def ping(engine: AsyncEngine, max_wait_s: float = 2.0) -> bool:
    """True when ``SELECT 1`` answers within ``max_wait_s`` (readiness, D-53)."""
    try:
        async with asyncio.timeout(max_wait_s), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
