"""The plan of the ``similar`` query on a real ``failures`` table (SPEC §6, D-79).

D-79 says the default query is an exact search per repository: the inner query orders by
``(embedding <=> :q) + 0``, an expression the HNSW index cannot serve, so the planner reads the
rows of the repository and sorts them. That was a hypothesis the spike never ran. These tests
check it with ``EXPLAIN`` on a table where one repository holds most of the rows, which is where
the planner prefers the HNSW index (and loses rows to the repository filter).
"""

import json
import random
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.postgres import store as store_module
from cicd_doctor.adapters.postgres.store import _SIMILAR_EXACT, PostgresFailureStore
from tests.factories import T0
from tests.pg import libpq_url

BIG = 1
SMALL = (2, 3)
BIG_ROWS = 4000  # 80 % of the table, well over the 15 % that tempts the planner
SMALL_ROWS = 500
DIMENSIONS = 384
INDEX = "failures_emb_idx"


def _vector(rng: random.Random) -> str:
    return "[" + ",".join(f"{rng.gauss(0, 1):.6f}" for _ in range(DIMENSIONS)) + "]"


def _fill(url: str) -> None:
    """Three repositories, one run each, ``BIG_ROWS`` / ``SMALL_ROWS`` failures with a published
    diagnosis, then ANALYZE (the planner needs the statistics a real table has)."""
    rng = random.Random(7)
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            "INSERT INTO installations (id, account_login, account_type) VALUES (1, 'a', 'O')"
        )
        for repo, count in [(BIG, BIG_ROWS), *((r, SMALL_ROWS) for r in SMALL)]:
            conn.execute(
                "INSERT INTO repositories (id, installation_id, full_name, private, default_branch)"
                " VALUES (%s, 1, %s, false, 'main')",
                (repo, f"acme/repo{repo}"),
            )
            conn.execute(
                "INSERT INTO workflow_runs (id, repository_id, workflow_id, workflow_name,"
                " head_sha, head_branch, head_repo_full_name, event, is_fork)"
                " VALUES (%s, %s, 1, 'CI', %s, 'main', %s, 'push', false)",
                (repo, repo, "a" * 40, f"acme/repo{repo}"),
            )
            conn.execute(
                "INSERT INTO run_attempts (run_id, attempt, conclusion, completed_at)"
                " VALUES (%s, 1, 'failure', now())",
                (repo,),
            )
            with conn.cursor().copy(
                "COPY failures (repository_id, run_id, run_attempt, job_id, job_name,"
                " signature_hash, signature_text, excerpt, excerpt_tokens, embedding, conclusion,"
                " started_at, completed_at) FROM STDIN"
            ) as copy:
                for job in range(count):
                    copy.write_row(
                        (
                            repo,
                            repo,
                            1,
                            job,
                            "tests",
                            "f" * 64,
                            "sig",
                            "[]",
                            1,
                            _vector(rng),
                            "failure",
                            "2026-01-01",
                            "2026-01-01",
                        )
                    )
        conn.execute(
            "INSERT INTO diagnoses (failure_id, source, published, category, probable_cause,"
            " evidence, suggested_fix, confidence)"
            " SELECT id, 'rules', true, 'unknown', 'cause', '[]', 'fix', 0.5 FROM failures"
        )
        conn.execute("ANALYZE failures")
        conn.execute("ANALYZE diagnoses")
        conn.execute("ANALYZE workflow_runs")


@pytest.fixture(scope="module")
def table(pg_url: str) -> Iterator[str]:
    from tests.pg import reset_tables

    reset_tables(pg_url)
    _fill(pg_url)
    yield pg_url
    reset_tables(pg_url)


@pytest.fixture
async def store(table: str) -> AsyncIterator[PostgresFailureStore]:
    async with PostgresFailureStore.connect(table, FakeClock(T0)) as postgres:
        yield postgres


@pytest.fixture
def query() -> str:
    return _vector(random.Random(99))


def _explain(url: str, statement: str, params: dict[str, Any]) -> str:
    """The JSON plan of ``statement`` (named ``:parameters``) with literal values inlined."""
    names = {name: f"%({name})s" for name in params}
    rendered = statement
    for name in sorted(names, key=len, reverse=True):
        rendered = rendered.replace(f":{name}", names[name])
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        row = conn.execute("EXPLAIN (FORMAT JSON) " + rendered, params)
        return json.dumps(row.fetchone()[0])  # type: ignore[index]


def _params(query: str, repo: int, k: int = 3, run: int = -1) -> dict[str, Any]:
    return {
        "q": query,
        "repo_id": repo,
        "fetch": 4 * k,
        "k": k,
        "exclude_run_id": run,
        "min_score": -1.0,
    }


def test_the_default_query_does_not_use_the_hnsw_index(table: str, query: str) -> None:
    """The hypothesis of D-79: ordering by ``(embedding <=> q) + 0`` keeps the planner off the
    HNSW index, also in the repository that holds 80 % of the table."""
    plan = _explain(table, _SIMILAR_EXACT.text, _params(query, BIG))

    assert INDEX not in plan


def test_the_naive_ordering_does_use_the_hnsw_index(table: str, query: str) -> None:
    """The control: without the ``+ 0`` the planner takes the index, so the test above can fail."""
    naive = (
        "SELECT id FROM failures WHERE repository_id = %(repo_id)s AND embedding IS NOT NULL "
        "ORDER BY embedding <=> CAST(%(q)s AS vector) LIMIT 12"
    )
    with psycopg.connect(libpq_url(table), connect_timeout=5) as conn:
        row = conn.execute("EXPLAIN (FORMAT JSON) " + naive, {"q": query, "repo_id": BIG})
        plan = json.dumps(row.fetchone()[0])  # type: ignore[index]

    assert INDEX in plan


def _exact_top(url: str, query: str, repo: int, k: int) -> list[Any]:
    """The oracle: a sequential scan with every index off."""
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        rows = conn.execute(
            "SELECT id FROM failures WHERE repository_id = %s "
            "ORDER BY embedding <=> CAST(%s AS vector), id LIMIT %s",
            (repo, query, k),
        )
        return [row[0] for row in rows]


@pytest.mark.parametrize("repo", [BIG, *SMALL])
async def test_the_result_is_the_exact_top_k(
    table: str, store: PostgresFailureStore, query: str, repo: int
) -> None:
    vector = [float(x) for x in query.strip("[]").split(",")]

    found = await store.similar(repo, vector, k=5, min_score=-1.0)

    assert [item.failure_id for item in found] == _exact_top(table, query, repo, 5)


async def test_it_never_returns_fewer_than_k_when_rows_exist(
    store: PostgresFailureStore, query: str
) -> None:
    vector = [float(x) for x in query.strip("[]").split(",")]

    for repo in (BIG, *SMALL):
        assert len(await store.similar(repo, vector, k=10, min_score=-1.0)) == 10


# --- the opt-in HNSW path (SIMILAR_USE_HNSW) ------------------------------------------------


def _row_vector(url: str, repo: int) -> list[float]:
    """The embedding of one stored row: an easy query, whose nearest neighbour is itself."""
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT embedding::text FROM failures WHERE repository_id = %s ORDER BY id LIMIT 1",
            (repo,),
        ).fetchone()
    assert row is not None
    return [float(x) for x in row[0].strip("[]").split(",")]


async def test_the_hnsw_path_agrees_with_the_exact_one_on_the_easy_case(table: str) -> None:
    vector = _row_vector(table, BIG)
    async with PostgresFailureStore.connect(table, FakeClock(T0), use_hnsw=True) as hnsw:
        approximate = await hnsw.similar(BIG, vector, k=1, min_score=0.5)
        full = await hnsw.similar(BIG, vector, k=10, min_score=-1.0)
    async with PostgresFailureStore.connect(table, FakeClock(T0)) as exact:
        expected = await exact.similar(BIG, vector, k=1, min_score=0.5)

    assert approximate == expected
    assert approximate[0].score == pytest.approx(1.0)
    assert len(full) == 10  # the iterative scan keeps going until the repository filter is met


async def test_the_hnsw_settings_do_not_leak_to_the_next_user_of_the_connection(
    table: str, query: str
) -> None:
    vector = [float(x) for x in query.strip("[]").split(",")]
    async with PostgresFailureStore.connect(
        table, FakeClock(T0), pool_size=1, use_hnsw=True
    ) as hnsw:
        await hnsw.similar(BIG, vector, k=3, min_score=-1.0)
        async with hnsw._engine.connect() as conn:  # the same pooled connection
            setting = (await conn.execute(text("SHOW hnsw.iterative_scan"))).scalar()
            ef_search = (await conn.execute(text("SHOW hnsw.ef_search"))).scalar()

    assert (setting, ef_search) == ("off", "40")


async def test_an_old_pgvector_keeps_the_exact_search(
    table: str, query: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(store_module, "_VECTOR_VERSION", text("SELECT '0.7.4'"))
    vector = [float(x) for x in query.strip("[]").split(",")]
    async with PostgresFailureStore.connect(table, FakeClock(T0), use_hnsw=True) as old:
        found = await old.similar(BIG, vector, k=5, min_score=-1.0)
        assert old._hnsw_supported is False

    assert [item.failure_id for item in found] == _exact_top(table, query, BIG, 5)
    assert "no iterative scans" in caplog.text


@pytest.mark.parametrize(
    ("version", "enough"),
    [
        ("0.8.7", True),
        ("0.10.0", True),
        ("1.0", True),
        ("0.7.4", False),
        (None, False),
        ("x", False),
    ],
)
def test_the_version_check(version: str | None, enough: bool) -> None:
    assert store_module._version_at_least(version, (0, 8)) is enough
