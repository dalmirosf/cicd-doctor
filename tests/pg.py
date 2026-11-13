"""Postgres test databases (D-51).

Tests never touch the development database: they use ``<db>_test`` (and ``<db>_test_migrations``
for the migration round trip), created on first use. The URL comes from ``TEST_DATABASE_URL``,
else from ``DATABASE_URL`` (``.env``) with ``_test`` appended to the database name.

When the server is unreachable the tests are skipped locally, with the command to start it, but
fail in CI (``CI`` is set by GitHub Actions), so a broken service container cannot go unnoticed.
"""

import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_URL = "postgresql+psycopg://cicd:cicd@127.0.0.1:5433/cicd_doctor_test"


def database_url_for_tests(suffix: str = "") -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        from cicd_doctor.app.settings import Settings

        try:
            base = make_url(Settings().database_url.get_secret_value())
            url = base.set(database=f"{base.database}_test").render_as_string(hide_password=False)
        except ValidationError:  # no DATABASE_URL anywhere
            url = FALLBACK_URL
    if suffix:
        parsed = make_url(url)
        url = parsed.set(database=f"{parsed.database}{suffix}").render_as_string(
            hide_password=False
        )
    return url


def libpq_url(url: str, database: str | None = None) -> str:
    parsed = make_url(url).set(drivername="postgresql")
    if database is not None:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


def ensure_database(url: str) -> None:
    """Create the database if it does not exist, or skip/fail when Postgres is unreachable."""
    name = make_url(url).database
    try:
        with psycopg.connect(
            libpq_url(url, "postgres"), autocommit=True, connect_timeout=5
        ) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if exists.fetchone() is None:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(str(name))))
    except psycopg.OperationalError as exc:
        if os.environ.get("CI"):
            raise
        parsed = make_url(url)
        pytest.skip(
            f"Postgres is not reachable at {parsed.host}:{parsed.port} ({type(exc).__name__}). "
            "Start it with: docker compose up -d db"
        )


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    config.attributes["configure_logger"] = False
    return config


def migrate_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def reset_tables(url: str) -> None:
    """Empty the tables the tests write to (CASCADE reaches everything that references them)."""
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "TRUNCATE jobs, webhook_deliveries, installations, repositories "
            "RESTART IDENTITY CASCADE"
        )


def scalar(url: str, query: str) -> object:
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        row = conn.execute(query).fetchone()
    return None if row is None else row[0]
