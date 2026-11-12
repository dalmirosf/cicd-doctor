"""Contract suite for the ``FailureStore`` port (SPEC §12).

Every adapter must pass these tests. For now only the in-memory adapter is parametrized; the
Postgres adapter joins the ``params`` list in P1 and must behave the same.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import FakeEmbedder, InMemoryFailureStore
from cicd_doctor.core.models import (
    Failure,
    FailureStatus,
    Publication,
    PublicationKey,
)
from cicd_doctor.core.ports import FailureStore
from tests.factories import T0, make_diagnosis, make_failure, make_run_ref


@dataclass
class Harness:
    store: FailureStore
    clock: FakeClock


def _memory() -> Harness:
    clock = FakeClock(T0)
    return Harness(store=InMemoryFailureStore(clock=clock), clock=clock)


_FACTORIES: dict[str, Callable[[], Harness]] = {"memory": _memory}


@pytest.fixture(params=list(_FACTORIES))
def harness(request: pytest.FixtureRequest) -> Harness:
    return _FACTORIES[request.param]()


# --- open failures and resolution ---------------------------------------------------------


async def test_open_failures_are_found_by_repo_workflow_and_branch(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await harness.store.save_failure(failure)

    found = await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key)

    assert [f.id for f in found] == [failure.id]


async def test_open_failures_do_not_leak_across_repo_workflow_or_branch(harness: Harness) -> None:
    run = make_run_ref()
    await harness.store.save_failure(make_failure(run=run))

    assert await harness.store.open_failures(999, run.workflow_id, run.branch_key) == []
    assert await harness.store.open_failures(run.repo_id, 999, run.branch_key) == []
    assert await harness.store.open_failures(run.repo_id, run.workflow_id, "acme/widgets:x") == []


async def test_resolve_closes_failures_and_records_the_fix_commit(harness: Harness) -> None:
    run = make_run_ref()
    failure = make_failure(run=run)
    await harness.store.save_failure(failure)

    await harness.store.resolve([failure.id], FailureStatus.RESOLVED_BY_COMMIT, "b" * 40)

    assert await harness.store.open_failures(run.repo_id, run.workflow_id, run.branch_key) == []


# --- exact-signature cache ----------------------------------------------------------------


async def test_cached_diagnosis_returns_a_recent_llm_diagnosis(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await harness.store.save_failure(failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis(source="llm"))
    harness.clock.advance(timedelta(days=6))

    cached = await harness.store.cached_diagnosis(
        failure.job.job.run.repo_id, "1" * 64, timedelta(days=7)
    )

    assert cached is not None
    assert cached.source == "llm"


async def test_cached_diagnosis_expires_after_max_age(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await harness.store.save_failure(failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis(source="llm"))
    harness.clock.advance(timedelta(days=8))

    assert (
        await harness.store.cached_diagnosis(
            failure.job.job.run.repo_id, "1" * 64, timedelta(days=7)
        )
        is None
    )


async def test_cached_diagnosis_ignores_rules_diagnoses(harness: Harness) -> None:
    failure = make_failure(signature_hash="1" * 64)
    await harness.store.save_failure(failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis(source="rules"))

    assert (
        await harness.store.cached_diagnosis(
            failure.job.job.run.repo_id, "1" * 64, timedelta(days=7)
        )
        is None
    )


async def test_cached_diagnosis_is_isolated_by_repository(harness: Harness) -> None:
    """D-10: a diagnosis from one repository must never be served to another."""
    failure = make_failure(run=make_run_ref(repo_id=100), signature_hash="1" * 64)
    await harness.store.save_failure(failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis(source="llm"))

    assert await harness.store.cached_diagnosis(200, "1" * 64, timedelta(days=7)) is None


# --- similarity ---------------------------------------------------------------------------


async def _failure_with_text(
    harness: Harness, text: str, *, repo_id: int = 100, job_id: int = 1
) -> Failure:
    [embedding] = FakeEmbedder().embed([text])
    failure = make_failure(run=make_run_ref(repo_id=repo_id), job_id=job_id, embedding=embedding)
    await harness.store.save_failure(failure)
    await harness.store.save_diagnosis(failure.id, make_diagnosis())
    return failure


async def test_similar_returns_best_matches_first_above_min_score(harness: Harness) -> None:
    exact = await _failure_with_text(harness, "No module named requests in tests", job_id=1)
    near = await _failure_with_text(harness, "No module named numpy in tests", job_id=2)
    await _failure_with_text(harness, "Error acquiring the state lock backend", job_id=3)
    [query] = FakeEmbedder().embed(["No module named requests in tests"])

    results = await harness.store.similar(100, query, k=3, min_score=0.5)

    assert [r.failure_id for r in results] == [exact.id, near.id]
    assert results[0].score == pytest.approx(1.0)
    assert results[0].score > results[1].score >= 0.5


async def test_similar_honors_k(harness: Harness) -> None:
    await _failure_with_text(harness, "No module named requests", job_id=1)
    await _failure_with_text(harness, "No module named requests today", job_id=2)
    [query] = FakeEmbedder().embed(["No module named requests"])

    assert len(await harness.store.similar(100, query, k=1, min_score=0.0)) == 1


async def test_similar_is_isolated_by_repository(harness: Harness) -> None:
    """D-10: vector search must never return failures from another repository."""
    await _failure_with_text(harness, "No module named requests", repo_id=100)
    [query] = FakeEmbedder().embed(["No module named requests"])

    assert await harness.store.similar(200, query, k=3, min_score=0.0) == []


# --- publications (idempotent outputs) ----------------------------------------------------


def _publication(attempt: int, *, run_id: int = 5000, external_id: int = 1) -> Publication:
    return Publication(
        key=PublicationKey.comment(repo_id=100, pr_number=12, workflow_id=7),
        external_id=external_id,
        run_id=run_id,
        attempt=attempt,
    )


async def test_publication_round_trip(harness: Harness) -> None:
    pub = _publication(attempt=1)

    assert await harness.store.publication(pub.key) is None
    assert await harness.store.save_publication(pub) is True
    assert await harness.store.publication(pub.key) == pub


async def test_an_older_attempt_cannot_overwrite_a_newer_publication(harness: Harness) -> None:
    newer = _publication(attempt=2, external_id=22)
    await harness.store.save_publication(newer)

    accepted = await harness.store.save_publication(_publication(attempt=1, external_id=11))

    assert accepted is False
    assert await harness.store.publication(newer.key) == newer


async def test_a_newer_or_equal_attempt_replaces_the_publication(harness: Harness) -> None:
    await harness.store.save_publication(_publication(attempt=1, external_id=11))

    assert await harness.store.save_publication(_publication(attempt=1, external_id=11)) is True
    assert await harness.store.save_publication(_publication(attempt=2, external_id=22)) is True
    stored = await harness.store.publication(_publication(attempt=2).key)
    assert stored is not None
    assert stored.attempt == 2


async def test_a_comment_from_a_newer_run_wins_over_a_higher_attempt_of_an_older_run(
    harness: Harness,
) -> None:
    await harness.store.save_publication(_publication(attempt=3, run_id=5000))

    assert await harness.store.save_publication(_publication(attempt=1, run_id=5001)) is True
