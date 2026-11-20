"""The in-memory history of the ``llm+hints+similar`` variant (SPEC §11.2, D-86).

Production serves a failure from the cache and lists similar past failures out of what earlier runs
of the same repository left in the store. The eval has no database, so it rebuilds that store as it
goes: the cases of one repository are processed in the order they were captured, every published
diagnosis goes into an ``InMemoryFailureStore`` with its embedding, and the next case searches
there. A case never finds itself (it is stored after it is diagnosed), and an ``injection`` case
has no history: nothing is looked up for it and nothing of it is kept.

The store is the one production uses (cache rules, TTL, prompt version, fork exclusion, score
threshold), asked with the defaults of ``DiagnoseConfig``. The clock follows ``captured_at``, so the
cache TTL is respected the way it would have been.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cicd_doctor.adapters.memory.store import InMemoryFailureStore
from cicd_doctor.core.analysis import JobAnalysis
from cicd_doctor.core.diagnosis.cache import reuse_cached
from cicd_doctor.core.diagnosis.pipeline import JobDiagnosis
from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.models import (
    CachedDiagnosis,
    Failure,
    FailureStatus,
    RunAnalysisRecord,
    RunAttempt,
    SimilarFailure,
    StoredFailure,
    WorkflowRun,
    failure_id_for,
)
from cicd_doctor.core.ports import Embedder
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig
from evals.runner.cases import Case

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_DEFAULTS = DiagnoseConfig()


class SteppedClock:
    """A clock that moves only when the history sets it, never backwards."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def move_to(self, moment: datetime) -> None:
        self._now = max(self._now, moment)


@dataclass(frozen=True)
class Lookup:
    """What the history found for one case, before the diagnosis."""

    cached: CachedDiagnosis | None
    similar: list[SimilarFailure]
    embedding: list[float] | None


def history_order(cases: Sequence[Case]) -> list[Case]:
    """The order the cases are processed in: per repository, by when they happened, then by id.

    That is ``provenance.occurred_at`` for a public case and ``captured_at`` for the others
    (D-101). A case with no date goes after the dated ones of its repository (the clock stays
    where it was). Repositories do not see each other, so their relative order does not matter.
    """
    return sorted(
        cases,
        key=lambda c: (
            c.run_ref().repo_full_name,
            c.meta.occurred_at is None,
            c.meta.occurred_at or EPOCH,
            c.id,
        ),
    )


def has_history(case: Case) -> bool:
    return case.meta.source != "injection"


@dataclass
class _Repo:
    clock: SteppedClock = field(default_factory=SteppedClock)
    store: InMemoryFailureStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = InMemoryFailureStore(self.clock)


class SimilarHistory:
    """One in-memory store per repository. Create one per variant run and model."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._repos: dict[str, _Repo] = {}

    def _repo(self, case: Case) -> _Repo:
        repo = self._repos.setdefault(case.run_ref().repo_full_name, _Repo())
        if case.meta.occurred_at is not None:
            repo.clock.move_to(case.meta.occurred_at)
        return repo

    async def lookup(self, case: Case, analysis: JobAnalysis) -> Lookup:
        """The cached diagnosis and the similar failures of a case, as production asks for them."""
        if not has_history(case):
            return Lookup(None, [], None)
        repo = self._repo(case)
        [embedding] = await asyncio.to_thread(self._embedder.embed, [analysis.signature.text])
        cached: CachedDiagnosis | None = None
        if _DEFAULTS.cache_ttl_days > 0:
            hit = await repo.store.cached_diagnosis(
                0,
                analysis.signature.hash,
                timedelta(days=_DEFAULTS.cache_ttl_days),
                PROMPT_VERSION,
            )
            reused = reuse_cached(hit.diagnosis, analysis.excerpt) if hit else None
            cached = (
                CachedDiagnosis(diagnosis=reused, origin_id=hit.origin_id)
                if (hit and reused)
                else None
            )
        similar = await repo.store.similar(
            0,
            embedding,
            _DEFAULTS.similar_k,
            _DEFAULTS.similar_min_score,
            exclude_run_id=case.run_ref().run_id or None,
        )
        return Lookup(cached, similar, embedding)

    async def remember(
        self, case: Case, analysis: JobAnalysis, result: JobDiagnosis, lookup: Lookup
    ) -> None:
        """Keep the published diagnosis of a case for the cases that come after it."""
        if not has_history(case):
            return
        repo = self._repo(case)
        run = case.run_ref()
        job = analysis.job.job
        stored = StoredFailure(
            failure=Failure(
                id=failure_id_for(run.run_id, run.run_attempt, job.job_id),
                job=analysis.job,
                excerpt=analysis.excerpt,
                signature=analysis.signature,
                embedding=lookup.embedding,
                status=FailureStatus.OPEN,
                fix_commit_sha=None,
            ),
            published=result.published,
            rules=result.rules,
            usage=result.usage,
            cached_from=result.cached_from,
        )
        await repo.store.save_run_analysis(
            RunAnalysisRecord(
                run=WorkflowRun(
                    id=run.run_id,
                    repo_id=0,
                    workflow_id=run.workflow_id,
                    workflow_name=run.workflow_name,
                    head_sha=run.head_sha,
                    head_branch=run.head_branch,
                    head_repo_full_name=run.head_repo_full_name,
                    event=run.event,
                    is_fork=False,
                    pr_number=None,
                ),
                attempt=RunAttempt(
                    run_id=run.run_id,
                    attempt=run.run_attempt,
                    conclusion="failure",
                    completed_at=repo.clock.now(),
                ),
                items=[stored],
            )
        )
