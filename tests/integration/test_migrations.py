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
    "diagnoses_published_idx",  # 0003
    "command_invocations_pr_idx",
}
# What 0003 adds, as (table, column) -> (data_type, is_nullable) (D-69).
COLUMNS_0003 = {
    ("failures", "conclusion"): ("text", "NO"),
    ("failures", "runner_os"): ("text", "YES"),
    ("failures", "started_at"): ("timestamp with time zone", "NO"),
    ("failures", "completed_at"): ("timestamp with time zone", "NO"),
    ("failures", "step_started_at"): ("timestamp with time zone", "YES"),
    ("failures", "step_completed_at"): ("timestamp with time zone", "YES"),
    ("run_attempts", "analyzed_at"): ("timestamp with time zone", "YES"),
    ("diagnoses", "llm_calls"): ("integer", "YES"),
    ("diagnoses", "llm_failed_calls"): ("integer", "YES"),
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


def _columns_0003(url: str) -> dict[tuple[str, str], tuple[str, str]]:
    """The columns of ``COLUMNS_0003`` that exist, with their type and nullability."""
    rows = _rows(
        url,
        """
        SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name IN ('failures', 'run_attempts', 'diagnoses')
        """,
    )
    return {(t, c): (kind, nullable) for t, c, kind, nullable in rows if (t, c) in COLUMNS_0003}


def _confidence_type(url: str) -> list[tuple[Any, ...]]:
    return _rows(
        url,
        """
        SELECT format_type(atttypid, atttypmod) FROM pg_attribute
        WHERE attrelid = 'diagnoses'::regclass AND attname = 'confidence'
        """,
    )


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
    assert _columns_0003(migrations_url) == COLUMNS_0003
    assert _confidence_type(migrations_url) == [("double precision",)]  # 0003: exact, not rounded
    published = str(indexes["diagnoses_published_idx"])
    assert "CREATE UNIQUE INDEX" in published
    assert "(failure_id) WHERE published" in published  # one published diagnosis per failure


def test_0003_rolls_back_to_0002_and_forward_again(migrations_url: str) -> None:
    config = alembic_config(migrations_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")

    assert _columns_0003(migrations_url) == {}
    assert _confidence_type(migrations_url) == [("numeric(3,2)",)]
    indexes = _pairs(migrations_url, "SELECT indexname, indexdef FROM pg_indexes")
    assert "diagnoses_published_idx" not in indexes
    assert "diagnoses_failure_idx" in indexes  # the rest of the schema is untouched
    command.upgrade(config, "head")
    assert _columns_0003(migrations_url) == COLUMNS_0003


def test_downgrade_removes_everything_and_upgrade_works_again(migrations_url: str) -> None:
    config = alembic_config(migrations_url)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert _tables(migrations_url) == set()
    assert _rows(migrations_url, "SELECT 1 FROM pg_type WHERE typname = 'job_status'") == []
    command.upgrade(config, "head")
    assert _tables(migrations_url) == TABLES
