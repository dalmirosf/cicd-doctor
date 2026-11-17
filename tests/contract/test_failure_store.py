"""Contract suite for the ``FailureStore`` port (SPEC §12, D-69, D-70).

Every adapter must pass these tests: the in-memory one and the Postgres one, whose parameter is
marked ``integration`` (D-51) so the fast suite stays free of a database.

The adapters are read through a small ``Harness``: ``prepare`` creates what the database needs
before a failure can be saved (installation, repository, run and attempt rows: the foreign keys),
and ``rows`` reads the ``diagnoses`` rows of a failure, which the port does not expose.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import FakeEmbedder, InMemoryFailureStore
from cicd_doctor.adapters.postgres.engine import make_engine
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.store import PostgresFailureStore
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import (
    Diagnosis,
    Failure,
    FailureStatus,
    Installation,
    Publication,
    PublicationKey,
    Repository,
    RunRef,
    StoredFailure,
    failure_id_for,
)
from cicd_doctor.core.ports import FailureStore
from tests.factories import (
    T0,
    make_diagnosis,
    make_failure,
    make_run_analysis,
    make_run_attempt,
    make_run_ref,
    make_stored_failure,
    make_workflow_run,
)

LOCK_WAIT = timedelta(milliseconds=300)


@dataclass(frozen=True)
class DiagnosisRow:
    """What a test needs to know about one ``diagnoses`` row."""

    source: str
    published: bool
    fallback_reason: str | None
    llm_calls: int | None
    cost_usd: Decimal | None


@dataclass
class Harness:
    store: FailureStore
    clock: FakeClock
    prepare: Callable[[RunRef], Awaitable[None]]
    rows: Callable[[UUID], Awaitable[list[DiagnosisRow]]]
    ids: Callable[[UUID], Awaitable[list[UUID]]]  # the ids of the diagnoses rows, oldest first


def _memory() -> Harness:
    clock = FakeClock(T0)
    store = InMemoryFailureStore(clock=clock, lock_wait=LOCK_WAIT)

    async def prepare(run: RunRef) -> None:
        await store.save_run(make_workflow_run(run), make_run_attempt(run))

    async def rows(failure_id: UUID) -> list[DiagnosisRow]:
        return [
            DiagnosisRow(
                row.diagnosis.source,
                row.published,
                row.diagnosis.fallback_reason,
                None if row.usage is None else row.usage.calls,
                None if row.usage is None else row.usage.cost_usd,
            )
            for row in store.diagnosis_rows(failure_id)
        ]

    async def ids(failure_id: UUID) -> list[UUID]:
        return [row.id for row in store.diagnosis_rows(failure_id)]

    return Harness(store=store, clock=clock, prepare=prepare, rows=rows, ids=ids)


@asynccontextmanager
async def _postgres(url: str) -> AsyncIterator[Harness]:
    clock = FakeClock(T0)
    engine = make_engine(url)
    store = PostgresFailureStore(engine, clock, lock_wait=LOCK_WAIT)
    installations = PostgresInstallationStore(engine)

    async def prepare(run: RunRef) -> None:
        installation = Installation(
            id=run.installation_id, account_login="acme", account_type="Organization"
        )
        repository = Repository(
            id=run.repo_id,
            installation_id=run.installation_id,
            full_name=run.repo_full_name,
            private=False,
            default_branch="main",
        )
        await installations.save_installation(installation)
        await installations.save_repositories([repository])
        await store.save_run(make_workflow_run(run), make_run_attempt(run))

    async def rows(failure_id: UUID) -> list[DiagnosisRow]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT source, published, fallback_reason, llm_calls, cost_usd
                    FROM diagnoses WHERE failure_id = :id ORDER BY created_at, published
                    """
                ),
                {"id": failure_id},
            )
        return [DiagnosisRow(*row) for row in result]

    async def ids(failure_id: UUID) -> list[UUID]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT id FROM diagnoses WHERE failure_id = :id ORDER BY created_at"),
                {"id": failure_id},
            )
        return [row[0] for row in result]

    try:
        yield Harness(store=store, clock=clock, prepare=prepare, rows=rows, ids=ids)
    finally:
        await engine.dispose()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    if request.param == "memory":
        yield _memory()
        return
    url: str = request.getfixturevalue("pg_clean")
    async with _postgres(url) as postgres:
        yield postgres


async def _save(harness: Harness, failure: Failure) -> None:
    await harness.prepare(failure.job.job.run)
    await harness.store.save_failure(failure)


# --- open failures and resolution ---------------------------------------------------------


async def test_open_failures_are_found_by_repo_workflow_and_branch(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await _save(harness, failure)

    found = await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key)

    assert [f.id for f in found] == [failure.id]


async def test_open_failures_do_not_leak_across_repo_workflow_or_branch(harness: Harness) -> None:
    run = make_run_ref()
    await _save(harness, make_failure(run=run))

    assert await harness.store.open_failures(999, run.workflow_id, run.branch_key) == []
    assert await harness.store.open_failures(run.repo_id, 999, run.branch_key) == []
    assert await harness.store.open_failures(run.repo_id, run.workflow_id, "acme/widgets:x") == []


async def test_a_saved_failure_comes_back_whole(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    job = failure.job.model_copy(
        update={
            "conclusion": "timed_out",
            "runner_os": None,
            "step_started_at": T0 + timedelta(minutes=1),
            "step_completed_at": T0 + timedelta(minutes=2),
        }
    )
    failure = failure.model_copy(update={"job": job})
    await _save(harness, failure)

    assert await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key) == [
        failure
    ]


async def test_a_failure_saved_twice_is_replaced(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await _save(harness, failure)
    changed = failure.model_copy(
        update={"signature": failure.signature.model_copy(update={"hash": "9" * 64})}
    )

    await harness.store.save_failure(changed)

    found = await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key)
    assert found == [changed]


async def test_an_embedding_is_stored_with_its_failure(harness: Harness) -> None:
    run = make_run_ref()
    embedding = [0.5, 0.25] * 192  # 384 dims, exact in 32-bit floats (pgvector keeps float4)
    failure = make_failure(run=run, embedding=embedding)
    await _save(harness, failure)

    found = await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key)

    assert [f.embedding for f in found] == [embedding]


async def test_resolve_closes_failures_and_records_the_fix_commit(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await _save(harness, failure)

    await harness.store.resolve([failure.id], FailureStatus.RESOLVED_BY_COMMIT, "b" * 40)

    assert await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key) == []


async def test_resolving_an_unknown_failure_changes_nothing(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await _save(harness, failure)

    with pytest.raises(KeyError):
        await harness.store.resolve(
            [failure.id, failure_id_for(1, 1, 1)], FailureStatus.RESOLVED_FLAKY, None
        )

    found = await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key)
    assert [f.id for f in found] == [failure.id]


async def test_a_diagnosis_for_an_unknown_failure_is_refused(harness: Harness) -> None:
    with pytest.raises(KeyError):
        await harness.store.save_diagnosis(failure_id_for(1, 1, 1), make_diagnosis())


# --- exact-signature cache ----------------------------------------------------------------

PROMPT = "diag-v1"
WEEK = timedelta(days=7)


def _llm(**changes: Any) -> Diagnosis:
    return make_diagnosis(**{"source": "llm", "prompt_version": PROMPT, **changes})


async def test_cached_diagnosis_returns_a_recent_llm_diagnosis(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())
    harness.clock.advance(timedelta(days=6))

    cached = await harness.store.cached_diagnosis(
        failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT
    )

    assert cached is not None
    assert cached.diagnosis.source == "llm"


async def test_cached_diagnosis_says_which_row_it_came_from(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())

    cached = await harness.store.cached_diagnosis(
        failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT
    )

    assert cached is not None
    assert [cached.origin_id] == await harness.ids(failure.id)


async def test_cached_diagnosis_expires_after_max_age(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())
    harness.clock.advance(timedelta(days=8))

    assert (
        await harness.store.cached_diagnosis(failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT)
        is None
    )


@pytest.mark.parametrize("source", ["rules", "cache"])
async def test_cached_diagnosis_only_serves_llm_rows(harness: Harness, source: str) -> None:
    """A ``cache`` row is never served again: the TTL runs from the real call (D-81)."""
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm(source=source))

    assert (
        await harness.store.cached_diagnosis(failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT)
        is None
    )


async def test_cached_diagnosis_skips_unverified_evidence(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm(evidence_verified=False))

    assert (
        await harness.store.cached_diagnosis(failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT)
        is None
    )


async def test_cached_diagnosis_is_tied_to_the_prompt_version(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())
    repo_id = failure.job.job.run.repo_id

    assert await harness.store.cached_diagnosis(repo_id, "1" * 64, WEEK, "diag-v2") is None
    assert await harness.store.cached_diagnosis(repo_id, "1" * 64, WEEK, PROMPT) is not None


async def test_cached_diagnosis_never_comes_from_a_fork(harness: Harness) -> None:
    """D-81: a fork controls its logs, so what the model said about them must not be served to
    normal runs."""
    failure = make_failure(run=make_run_ref(is_fork=True), signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())

    assert (
        await harness.store.cached_diagnosis(failure.job.job.run.repo_id, "1" * 64, WEEK, PROMPT)
        is None
    )


async def test_cached_diagnosis_is_isolated_by_repository(harness: Harness) -> None:
    """D-10: a diagnosis from one repository must never be served to another."""
    failure = make_failure(run=make_run_ref(repo_id=100), signature_hash="1" * 64)
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, _llm())

    assert await harness.store.cached_diagnosis(200, "1" * 64, WEEK, PROMPT) is None


async def test_the_llm_diagnosis_of_a_checkpoint_feeds_the_cache(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    await harness.prepare(run)
    await harness.store.save_run_analysis(record)
    published = record.items[0].published  # the LLM one; the others fell back to the rules
    assert published.prompt_version == "v1"

    cached = await harness.store.cached_diagnosis(run.repo_id, "f" * 64, WEEK, "v1")

    assert cached is not None
    assert cached.diagnosis == published


# --- similarity (D-79) --------------------------------------------------------------------


def _embedding(text: str) -> list[float]:
    [embedding] = FakeEmbedder().embed([text])
    return embedding


async def _publish(
    harness: Harness,
    text: str,
    *,
    repo_id: int = 100,
    job_id: int = 1,
    run_id: int | None = None,
    is_fork: bool = False,
    embedded: bool = True,
    cause: str | None = None,
) -> StoredFailure:
    """A failure of its own run, with the embedding of ``text`` and a published diagnosis."""
    run = make_run_ref(
        repo_id=repo_id, run_id=run_id or repo_id * 100_000 + job_id, is_fork=is_fork
    )
    published = make_diagnosis(
        model="gpt-test", prompt_version="v1", probable_cause=cause or f"cause of {text}"
    )
    item = make_stored_failure(
        run=run,
        job_id=job_id,
        published=published,
        embedding=_embedding(text) if embedded else None,
    )
    await harness.prepare(run)
    await harness.store.save_run_analysis(make_run_analysis(run, [item]))
    return item


async def test_similar_returns_best_matches_first_above_min_score(harness: Harness) -> None:
    exact = await _publish(harness, "No module named requests in tests", job_id=1)
    near = await _publish(harness, "No module named numpy in tests", job_id=2)
    await _publish(harness, "Error acquiring the state lock backend", job_id=3)

    results = await harness.store.similar(
        100, _embedding("No module named requests in tests"), k=3, min_score=0.5
    )

    assert [r.failure_id for r in results] == [exact.failure.id, near.failure.id]
    assert results[0].score == pytest.approx(1.0)
    assert results[0].score > results[1].score >= 0.5


async def test_similar_honors_k(harness: Harness) -> None:
    await _publish(harness, "No module named requests", job_id=1)
    await _publish(harness, "No module named requests today", job_id=2)
    query = _embedding("No module named requests")

    assert len(await harness.store.similar(100, query, k=1, min_score=0.0)) == 1
    assert len(await harness.store.similar(100, query, k=5, min_score=0.0)) == 2
    assert await harness.store.similar(100, query, k=0, min_score=0.0) == []


async def test_similar_rounds_the_score_to_three_decimals(harness: Harness) -> None:
    await _publish(harness, "No module named numpy in tests")

    [result] = await harness.store.similar(
        100, _embedding("No module named requests in tests"), k=3, min_score=0.0
    )

    assert result.score == round(result.score, 3)


async def test_similar_is_isolated_by_repository(harness: Harness) -> None:
    """D-10: the same vector in two repositories; each search sees only its own."""
    mine = await _publish(harness, "No module named requests", repo_id=100)
    theirs = await _publish(harness, "No module named requests", repo_id=200)
    query = _embedding("No module named requests")

    found_mine = await harness.store.similar(100, query, 3, 0.0)
    found_theirs = await harness.store.similar(200, query, 3, 0.0)

    assert [r.failure_id for r in found_mine] == [mine.failure.id]
    assert [r.failure_id for r in found_theirs] == [theirs.failure.id]
    assert await harness.store.similar(300, query, 3, 0.0) == []


async def test_similar_is_not_crowded_out_by_closer_rows_of_other_repositories(
    harness: Harness,
) -> None:
    """The case that breaks a filter applied after the vector search: 1 000 rows of another
    repository sit closer to the query than the ones of this repository."""
    query = _embedding("No module named requests")
    mine = [
        await _publish(harness, "No module named numpy", job_id=1),
        await _publish(harness, "No module named scipy", job_id=2),
        await _publish(harness, "No module named pandas", job_id=3),
    ]
    other = make_run_ref(repo_id=200, run_id=900_000)
    crowd = [
        make_stored_failure(
            run=other,
            job_id=job_id,
            embedding=query,
            published=make_diagnosis(model="gpt-test", prompt_version="v1"),
        )
        for job_id in range(1, 1001)
    ]
    await harness.prepare(other)
    await harness.store.save_run_analysis(make_run_analysis(other, crowd))

    results = await harness.store.similar(100, query, k=3, min_score=0.0)

    assert {r.failure_id for r in results} == {item.failure.id for item in mine}
    assert len(await harness.store.similar(200, query, k=3, min_score=0.0)) == 3


async def test_similar_never_returns_failures_of_forks(harness: Harness) -> None:
    await _publish(harness, "No module named requests", job_id=1, is_fork=True)
    normal = await _publish(harness, "No module named requests", job_id=2)

    results = await harness.store.similar(100, _embedding("No module named requests"), 3, 0.0)

    assert [r.failure_id for r in results] == [normal.failure.id]


async def test_similar_leaves_out_the_run_it_is_asked_to_exclude(harness: Harness) -> None:
    await _publish(harness, "No module named requests", job_id=1, run_id=777)
    other = await _publish(harness, "No module named requests", job_id=2, run_id=778)
    query = _embedding("No module named requests")

    assert len(await harness.store.similar(100, query, 3, 0.0)) == 2
    results = await harness.store.similar(100, query, 3, 0.0, exclude_run_id=777)

    assert [r.failure_id for r in results] == [other.failure.id]


async def test_similar_shows_the_published_diagnosis_and_the_fix(harness: Harness) -> None:
    item = await _publish(harness, "No module named requests", cause="the published cause")
    await harness.store.resolve([item.failure.id], FailureStatus.RESOLVED_BY_COMMIT, "c" * 40)

    [result] = await harness.store.similar(100, _embedding("No module named requests"), 3, 0.0)

    run = item.failure.job.job.run
    assert result.probable_cause == "the published cause"  # not the rules row's
    assert result.category == item.published.category
    assert result.fix_commit_sha == "c" * 40
    assert (result.run_id, result.run_attempt) == (run.run_id, run.run_attempt)


async def test_similar_ignores_failures_without_a_published_diagnosis(harness: Harness) -> None:
    failure = make_failure(embedding=_embedding("No module named requests"))
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis())  # saved, not published

    assert await harness.store.similar(100, _embedding("No module named requests"), 3, 0.0) == []


async def test_similar_ignores_failures_without_an_embedding(harness: Harness) -> None:
    await _publish(harness, "No module named requests", embedded=False)

    assert await harness.store.similar(100, _embedding("No module named requests"), 3, 0.0) == []


# --- the checkpoint of a run attempt (D-69) -----------------------------------------------


async def test_a_run_analysis_round_trips_exactly(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    assert len(record.items) == 3
    await harness.prepare(run)

    saved = await harness.store.save_run_analysis(record)

    assert saved == record
    assert await harness.store.run_analysis(run.run_id, run.run_attempt) == record


async def test_the_first_analysis_wins_and_the_second_gets_what_is_stored(
    harness: Harness,
) -> None:
    run = make_run_ref()
    first = make_run_analysis(run)
    other = make_stored_failure(run=run, job_id=9)
    second = make_run_analysis(run, [other])
    await harness.prepare(run)
    await harness.store.save_run_analysis(first)

    returned = await harness.store.save_run_analysis(second)

    assert returned == first
    assert await harness.store.run_analysis(run.run_id, run.run_attempt) == first
    assert await harness.rows(other.failure.id) == []  # nothing of the loser was written
    assert [len(await harness.rows(item.failure.id)) for item in first.items] == [2, 1, 1]


async def test_concurrent_analyses_of_one_attempt_agree(harness: Harness) -> None:
    run = make_run_ref()
    first = make_run_analysis(run)
    second = make_run_analysis(run, [make_stored_failure(run=run, job_id=9)])
    await harness.prepare(run)

    results = await asyncio.gather(
        harness.store.save_run_analysis(first), harness.store.save_run_analysis(second)
    )

    stored = await harness.store.run_analysis(run.run_id, run.run_attempt)
    assert results[0] == results[1] == stored
    assert stored in (first, second)


async def test_an_attempt_that_was_not_analysed_has_no_checkpoint(harness: Harness) -> None:
    run = make_run_ref()
    assert await harness.store.run_analysis(run.run_id, run.run_attempt) is None

    failure = make_failure(run=run)  # saved row by row: a half-written analysis is no checkpoint
    await _save(harness, failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis())

    assert await harness.store.run_analysis(run.run_id, run.run_attempt) is None


async def test_an_attempt_without_failures_is_a_valid_checkpoint(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run, [])
    await harness.prepare(run)

    saved = await harness.store.save_run_analysis(record)

    assert saved == record
    assert saved.items == []
    assert await harness.store.run_analysis(run.run_id, run.run_attempt) == record


async def test_every_attempt_has_its_own_checkpoint(harness: Harness) -> None:
    attempt_1, attempt_2 = make_run_ref(run_attempt=1), make_run_ref(run_attempt=2)
    first = make_run_analysis(attempt_1, [make_stored_failure(run=attempt_1)])
    second = make_run_analysis(attempt_2, [make_stored_failure(run=attempt_2)])
    await harness.prepare(attempt_1)

    await harness.store.save_run_analysis(first)
    assert await harness.store.run_analysis(5000, 2) is None
    await harness.store.save_run_analysis(second)

    assert await harness.store.run_analysis(5000, 1) == first
    assert await harness.store.run_analysis(5000, 2) == second
    assert first.items[0].failure.id != second.items[0].failure.id


async def test_the_items_of_a_checkpoint_are_ordered_by_job_id(harness: Harness) -> None:
    run = make_run_ref()
    items = [make_stored_failure(run=run, job_id=job_id) for job_id in (30, 10, 20)]
    await harness.prepare(run)

    saved = await harness.store.save_run_analysis(make_run_analysis(run, items))

    assert [item.failure.job.job.job_id for item in saved.items] == [10, 20, 30]


async def test_exactly_one_diagnosis_is_published_per_failure(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    await harness.prepare(run)
    await harness.store.save_run_analysis(record)

    shapes = [
        [(row.source, row.published) for row in await harness.rows(item.failure.id)]
        for item in record.items
    ]

    # The rules row always exists; the LLM row only when the LLM answered validly. The rules
    # fallbacks are the one published row of their failure.
    assert shapes == [
        [("rules", False), ("llm", True)],
        [("rules", True)],
        [("rules", True)],
    ]


async def test_the_usage_sits_on_the_published_row(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    await harness.prepare(run)
    await harness.store.save_run_analysis(record)
    llm, billed_fallback, free_fallback = [await harness.rows(i.failure.id) for i in record.items]

    assert [(r.published, r.llm_calls, r.cost_usd) for r in llm] == [
        (False, None, None),
        (True, 2, Decimal("0.001234")),
    ]
    assert [(r.fallback_reason, r.llm_calls, r.cost_usd) for r in billed_fallback] == [
        ("invalid_output", 2, Decimal("0.000900"))  # the failed answers were paid for
    ]
    assert [(r.fallback_reason, r.llm_calls, r.cost_usd) for r in free_fallback] == [
        ("fork_policy", None, None)
    ]
    spent = sum(
        (r.cost_usd or Decimal(0) for rows in (llm, billed_fallback) for r in rows), Decimal(0)
    )
    assert spent == Decimal("0.002134")


async def test_a_cached_diagnosis_is_published_next_to_the_rules_one(harness: Harness) -> None:
    run = make_run_ref()
    rules = make_stored_failure(run=run).rules
    cached = make_diagnosis(source="cache", model="gpt-test", confidence=0.7)
    item = make_stored_failure(run=run, published=cached)
    await harness.prepare(run)

    saved = await harness.store.save_run_analysis(make_run_analysis(run, [item]))

    assert saved.items[0].published == cached
    assert saved.items[0].rules == rules
    assert saved.items[0].usage is None
    rows = await harness.rows(item.failure.id)
    assert [(r.source, r.published) for r in rows] == [("rules", False), ("cache", True)]


async def test_a_resolved_failure_shows_its_new_status_in_the_checkpoint(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    await harness.prepare(run)
    await harness.store.save_run_analysis(record)

    await harness.store.resolve(
        [record.items[0].failure.id], FailureStatus.RESOLVED_BY_COMMIT, "b" * 40
    )

    stored = await harness.store.run_analysis(run.run_id, run.run_attempt)
    assert stored is not None
    assert [(i.failure.status, i.failure.fix_commit_sha) for i in stored.items] == [
        (FailureStatus.RESOLVED_BY_COMMIT, "b" * 40),
        (FailureStatus.OPEN, None),
        (FailureStatus.OPEN, None),
    ]
    assert stored.items[0].published == record.items[0].published


async def test_an_analysed_attempt_is_not_rewritten_by_save_run(harness: Harness) -> None:
    run = make_run_ref()
    record = make_run_analysis(run)
    await harness.prepare(run)
    await harness.store.save_run_analysis(record)

    await harness.store.save_run(
        make_workflow_run(run), make_run_attempt(run, conclusion="timed_out")
    )

    stored = await harness.store.run_analysis(run.run_id, run.run_attempt)
    assert stored is not None
    assert stored.attempt.conclusion == "failure"


async def test_analysed_failures_belong_to_their_repository_only(harness: Harness) -> None:
    """D-10: what a checkpoint stores is invisible to the other repositories."""
    mine = make_run_ref(repo_id=100, run_id=5000)
    theirs = make_run_ref(repo_id=200, run_id=6000)
    await harness.prepare(mine)
    await harness.prepare(theirs)

    await harness.store.save_run_analysis(make_run_analysis(mine))

    assert len(await harness.store.open_failures(100, mine.workflow_id, mine.branch_key)) == 3
    assert await harness.store.open_failures(200, mine.workflow_id, mine.branch_key) == []
    assert await harness.store.cached_diagnosis(200, "f" * 64, WEEK, "v1") is None
    assert await harness.store.run_analysis(6000, 1) is None


# --- publications (idempotent outputs) ----------------------------------------------------


def _publication(attempt: int, *, run_id: int = 5000, external_id: int = 1) -> Publication:
    return Publication(
        key=PublicationKey.comment(repo_id=100, pr_number=12, workflow_id=7),
        external_id=external_id,
        run_id=run_id,
        attempt=attempt,
    )


def _check_publication(attempt: int, *, external_id: int = 1) -> Publication:
    return Publication(
        key=PublicationKey.check(repo_id=100, run_id=5000),
        external_id=external_id,
        run_id=5000,
        attempt=attempt,
    )


async def test_publication_round_trip(harness: Harness) -> None:
    await harness.prepare(make_run_ref())
    pub = _publication(attempt=1)

    assert await harness.store.publication(pub.key) is None
    assert await harness.store.save_publication(pub) is True
    assert await harness.store.publication(pub.key) == pub


async def test_a_check_publication_round_trip(harness: Harness) -> None:
    await harness.prepare(make_run_ref())
    pub = _check_publication(attempt=1)

    assert await harness.store.publication(pub.key) is None
    assert await harness.store.save_publication(pub) is True
    assert await harness.store.publication(pub.key) == pub


async def test_an_older_attempt_cannot_overwrite_a_newer_publication(harness: Harness) -> None:
    await harness.prepare(make_run_ref())
    newer = _publication(attempt=2, external_id=22)
    await harness.store.save_publication(newer)

    accepted = await harness.store.save_publication(_publication(attempt=1, external_id=11))

    assert accepted is False
    assert await harness.store.publication(newer.key) == newer


async def test_an_older_attempt_cannot_overwrite_a_newer_check_publication(
    harness: Harness,
) -> None:
    await harness.prepare(make_run_ref())
    newer = _check_publication(attempt=2, external_id=22)
    await harness.store.save_publication(newer)

    accepted = await harness.store.save_publication(_check_publication(attempt=1, external_id=11))

    assert accepted is False
    assert await harness.store.publication(newer.key) == newer


async def test_a_newer_or_equal_attempt_replaces_the_publication(harness: Harness) -> None:
    await harness.prepare(make_run_ref())
    await harness.store.save_publication(_publication(attempt=1, external_id=11))

    assert await harness.store.save_publication(_publication(attempt=1, external_id=11)) is True
    assert await harness.store.save_publication(_publication(attempt=2, external_id=22)) is True
    stored = await harness.store.publication(_publication(attempt=2).key)
    assert stored is not None
    assert stored.attempt == 2


async def test_a_comment_from_a_newer_run_wins_over_a_higher_attempt_of_an_older_run(
    harness: Harness,
) -> None:
    await harness.prepare(make_run_ref())
    await harness.store.save_publication(_publication(attempt=3, run_id=5000))

    assert await harness.store.save_publication(_publication(attempt=1, run_id=5001)) is True


async def test_an_older_run_cannot_overwrite_the_comment_of_a_newer_one(harness: Harness) -> None:
    await harness.prepare(make_run_ref())
    newer = _publication(attempt=1, run_id=5001, external_id=22)
    await harness.store.save_publication(newer)

    assert await harness.store.save_publication(_publication(attempt=9, run_id=5000)) is False
    assert await harness.store.publication(newer.key) == newer


async def test_publications_are_isolated_by_repository(harness: Harness) -> None:
    """D-10: the same PR number and workflow id in two repositories are two outputs."""
    await harness.prepare(make_run_ref(repo_id=100, run_id=5000))
    await harness.prepare(make_run_ref(repo_id=200, run_id=6000))
    mine = _publication(attempt=2, external_id=22)
    theirs = Publication(
        key=PublicationKey.comment(repo_id=200, pr_number=12, workflow_id=7),
        external_id=33,
        run_id=6000,
        attempt=1,
    )

    assert await harness.store.save_publication(mine) is True
    assert await harness.store.publication(theirs.key) is None
    assert await harness.store.save_publication(theirs) is True  # attempt 1 < 2 does not matter

    assert await harness.store.publication(mine.key) == mine
    assert await harness.store.publication(theirs.key) == theirs


# --- the publication lock (D-70) ----------------------------------------------------------

COMMENT_KEY = PublicationKey.comment(repo_id=100, pr_number=12, workflow_id=7)


async def test_the_second_owner_of_a_lock_waits_for_the_first(harness: Harness) -> None:
    events: list[str] = []

    async def second() -> None:
        async with harness.store.publication_lock(COMMENT_KEY):
            events.append("second has it")

    async with harness.store.publication_lock(COMMENT_KEY):
        waiting = asyncio.create_task(second())
        await asyncio.sleep(0.1)  # within the wait: it blocks instead of failing
        events.append("first is done")

    await waiting
    assert events == ["first is done", "second has it"]


async def test_a_lock_is_given_up_after_the_wait_with_a_transient_error(harness: Harness) -> None:
    async with harness.store.publication_lock(COMMENT_KEY):
        with pytest.raises(TransientError, match="busy"):
            async with harness.store.publication_lock(COMMENT_KEY):
                pytest.fail("the lock was given to a second owner")


async def test_locks_of_different_outputs_do_not_block_each_other(harness: Harness) -> None:
    other = PublicationKey.comment(repo_id=100, pr_number=13, workflow_id=7)
    check = PublicationKey.check(repo_id=100, run_id=5000)

    async with (
        harness.store.publication_lock(COMMENT_KEY),
        harness.store.publication_lock(other),
        harness.store.publication_lock(check),
    ):
        pass


async def test_a_lock_is_released_when_its_owner_fails(harness: Harness) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with harness.store.publication_lock(COMMENT_KEY):
            raise RuntimeError("boom")

    async with harness.store.publication_lock(COMMENT_KEY):
        pass
