"""The Alembic migration creates exactly the SPEC §6 schema and can be rolled back (D-50)."""

from typing import Any

import psycopg
import pytest
from alembic import command

from tests.pg import alembic_config, database_url_for_tests, ensure_database, libpq_url

TABLES = {
    "installations",
    "repositories",
    "webhook_deliveries",
    "jobs",
    "workflow_runs",
    "run_attempts",
    "failures",
    "diagnoses",
    "check_publications",
    "comment_publications",
    "llm_usage",
    "command_invocations",
}
ENUMS = {
    "job_status": ["queued", "running", "done", "failed", "dead"],
    "failure_category": [
        "broken_test",
        "dependency",
        "network",
        "config_or_secrets",
        "timeout",
        "infra_or_deploy",
        "flaky",
        "unknown",
    ],
    "diagnosis_source": ["rules", "llm", "cache"],
    "failure_status": ["open", "resolved_flaky", "resolved_by_commit", "stale"],
}
INDEXES = {
    "jobs_ready_idx",
    "workflow_runs_branch_idx",
    "failures_sig_idx",
    "failures_open_idx",
    "failures_emb_idx",
    "diagnoses_failure_idx",
    "command_invocations_pr_idx",
}


@pytest.fixture
def migrations_url(pg_url: str) -> str:
    """A database of its own, so rolling back does not disturb the other integration tests.

    Depends on ``pg_url`` only so that a server that is down skips once, not once per test.
    """
    url = database_url_for_tests("_migrations")
    ensure_database(url)
    command.downgrade(alembic_config(url), "base")
    return url


def _rows(url: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        return conn.execute(query).fetchall()


def _pairs(url: str, query: str) -> dict[Any, Any]:
    return dict(_rows(url, query))


def _tables(url: str) -> set[object]:
    rows = _rows(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {name for (name,) in rows} - {"alembic_version"}


def test_upgrade_creates_the_spec_schema(migrations_url: str) -> None:
    command.upgrade(alembic_config(migrations_url), "head")

    assert _tables(migrations_url) == TABLES
    enums = _pairs(
        migrations_url,
        """
        SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        GROUP BY t.typname
        """,
    )
    assert enums == ENUMS
    assert _rows(migrations_url, "SELECT 1 FROM pg_extension WHERE extname = 'vector'") == [(1,)]
    indexes = _pairs(
        migrations_url, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    )
    assert set(indexes) >= INDEXES
    assert "USING hnsw (embedding vector_cosine_ops)" in str(indexes["failures_emb_idx"])
    assert "WHERE (status = ANY" in str(indexes["jobs_ready_idx"])
    embedding = _rows(
        migrations_url,
        """
        SELECT format_type(atttypid, atttypmod) FROM pg_attribute
        WHERE attrelid = 'failures'::regclass AND attname = 'embedding'
        """,
    )
    assert embedding == [("vector(384)",)]
    default_branch = _rows(
        migrations_url,
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = 'repositories' AND column_name = 'default_branch'
        """,
    )
    assert default_branch == [("YES",)]  # 0002: installation events carry no default branch


def test_downgrade_removes_everything_and_upgrade_works_again(migrations_url: str) -> None:
    config = alembic_config(migrations_url)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert _tables(migrations_url) == set()
    assert _rows(migrations_url, "SELECT 1 FROM pg_type WHERE typname = 'job_status'") == []
    command.upgrade(config, "head")
    assert _tables(migrations_url) == TABLES
