"""The plans of the success-path queries on tables of a realistic shape (SPEC §6, D-83, 0006).

Migration 0006 adds no index for ``open_failures`` or ``later_successes``: the open failures are
read through the partial ``failures_open_*`` indexes (a few percent of the table) and the runs by
their primary key. Measured on this shape with 90 000 runs (the NFR-11 ceiling) and 1 777 open
failures on a branch, ``open_failures`` took 10 ms with a sequential scan of ``workflow_runs``
(the ``head_repo || ':' || head_branch`` expression cannot use ``workflow_runs_branch_idx``), which
is not worth an expression index on a table that every webhook writes. The module uses 30 000
runs on 60 branches, one failure per run and most of them resolved, to keep CI fast.
"""

import random
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from cicd_doctor.adapters.postgres.store import _LATER_SUCCESSES, _OPEN_FAILURES, _RESOLVED_BY
from tests.pg import libpq_url, reset_tables

RUNS = 30000
BRANCHES = 60


def _fill(url: str) -> None:
    rng = random.Random(3)
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "INSERT INTO installations (id, account_login, account_type) VALUES (1, 'a', 'O')"
        )
        conn.execute(
            "INSERT INTO repositories (id, installation_id, full_name, private, default_branch)"
            " VALUES (1, 1, 'acme/repo', false, 'main')"
        )
        with conn.cursor().copy(
            "COPY workflow_runs (id, repository_id, workflow_id, workflow_name, head_sha,"
            " head_branch, head_repo_full_name, event, is_fork) FROM STDIN"
        ) as copy:
            for run in range(1, RUNS + 1):
                copy.write_row(
                    (
                        run,
                        1,
                        7,
                        "CI",
                        f"{run:040x}",
                        f"b{run % BRANCHES}",
                        "acme/repo",
                        "push",
                        False,
                    )
                )
        with conn.cursor().copy(
            "COPY run_attempts (run_id, attempt, conclusion, completed_at) FROM STDIN"
        ) as copy:
            for run in range(1, RUNS + 1):
                copy.write_row((run, 1, "failure" if run % 3 else "success", "2026-11-01"))
        with conn.cursor().copy(
            "COPY failures (repository_id, run_id, run_attempt, job_id, job_name, signature_hash,"
            " signature_text, excerpt, excerpt_tokens, conclusion, started_at, completed_at,"
            " status, resolved_by_run_id, resolved_by_attempt) FROM STDIN"
        ) as copy:
            for run in range(1, RUNS + 1):
                if run % 3 == 0:
                    continue
                open_ = rng.random() < 0.03
                copy.write_row(
                    (1, run, 1, 1, "tests", "f" * 64, "sig", "[]", 1, "failure", "2026-11-01")
                    + ("2026-11-01", "open" if open_ else "resolved_by_commit")
                    + ((None, None) if open_ else (run + 1, 1))
                )
        for table in ("workflow_runs", "run_attempts", "failures"):
            conn.execute(f"ANALYZE {table}")


@pytest.fixture(scope="module")
def table(pg_url: str) -> Iterator[str]:
    reset_tables(pg_url)
    _fill(pg_url)
    yield pg_url
    reset_tables(pg_url)


def _plan(url: str, query: Any, params: dict[str, Any]) -> str:
    """``EXPLAIN`` of a SQLAlchemy ``text`` query: ``:name`` becomes psycopg's ``%(name)s``."""
    sql = re.sub(r"(?<![:\w]):(\w+)", r"%(\1)s", str(query.text))
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        rows = conn.execute("EXPLAIN (COSTS OFF) " + sql, params).fetchall()
    return "\n".join(row[0] for row in rows)


def test_open_failures_read_the_open_failures_through_a_partial_index(table: str) -> None:
    plan = _plan(
        table, _OPEN_FAILURES, {"repo_id": 1, "workflow_id": 7, "branch_key": "acme/repo:b5"}
    )

    assert "Seq Scan on failures" not in plan, plan
    assert "failures_open" in plan, plan


def test_later_successes_are_found_through_the_attempts_and_runs_keys(table: str) -> None:
    """A recent failure has few later successes: the cut ``(run_id, attempt) >`` is the key."""
    plan = _plan(
        table,
        _LATER_SUCCESSES,
        {
            "repo_id": 1,
            "workflow_id": 7,
            "branch_key": "acme/repo:b5",
            "after_run_id": RUNS - 10,
            "after_attempt": 1,
        },
    )

    assert "Index Scan using run_attempts_pkey" in plan, plan
    assert "Index Scan using workflow_runs_pkey" in plan, plan


def test_resolved_by_uses_its_partial_index(table: str) -> None:
    plan = _plan(table, _RESOLVED_BY, {"repo_id": 1, "run_id": 100, "attempt": 1})

    assert "failures_resolved_by_idx" in plan, plan
