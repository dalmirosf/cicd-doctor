"""In-memory ``FailureStore``: reference behaviour for the contract tests and offline runs."""

import asyncio
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import (
    CachedDiagnosis,
    Diagnosis,
    DiagnosisUsage,
    Failure,
    FailureStatus,
    Publication,
    PublicationKey,
    RunAnalysisRecord,
    RunAttempt,
    SimilarFailure,
    StoredFailure,
    WorkflowRun,
)
from cicd_doctor.core.ports import Clock

DEFAULT_LOCK_WAIT = timedelta(seconds=30)


@dataclass(frozen=True)
class StoredDiagnosis:
    """One ``diagnoses`` row: a diagnosis, whether it is the published one, and its usage."""

    diagnosis: Diagnosis
    created_at: datetime
    published: bool = False
    usage: DiagnosisUsage | None = None  # only on the published row (D-69)
    id: UUID = field(default_factory=uuid4)  # ``diagnoses.id``: the origin of a cache hit
    cached_from: UUID | None = None  # ``diagnoses.cached_from`` (D-81)


class InMemoryFailureStore:
    """Implements ``FailureStore``. Every lookup is scoped by ``repo_id`` (D-10)."""

    def __init__(
        self, clock: Clock | None = None, *, lock_wait: timedelta = DEFAULT_LOCK_WAIT
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._lock_wait = lock_wait
        self._runs: dict[int, WorkflowRun] = {}
        self._attempts: dict[tuple[int, int], RunAttempt] = {}
        self._failures: dict[UUID, Failure] = {}
        self._diagnoses: dict[UUID, list[StoredDiagnosis]] = {}
        self._publications: dict[PublicationKey, Publication] = {}
        # The checkpoints: (run id, attempt) -> the stored failures, as they were saved.
        self._analyses: dict[tuple[int, int], list[StoredFailure]] = {}
        self._locks: dict[PublicationKey, asyncio.Lock] = {}

    async def save_run(self, run: WorkflowRun, attempt: RunAttempt) -> None:
        self._runs[run.id] = run
        if (attempt.run_id, attempt.attempt) not in self._analyses:  # an analysed attempt is final
            self._attempts[(attempt.run_id, attempt.attempt)] = attempt

    async def save_failure(self, failure: Failure) -> None:
        self._failures[failure.id] = failure

    async def save_diagnosis(self, failure_id: UUID, diagnosis: Diagnosis) -> None:
        if failure_id not in self._failures:
            raise KeyError(f"unknown failure {failure_id}")
        stored = StoredDiagnosis(diagnosis, self._clock.now())
        self._diagnoses.setdefault(failure_id, []).append(stored)

    async def save_run_analysis(self, record: RunAnalysisRecord) -> RunAnalysisRecord:
        key = (record.run.id, record.attempt.attempt)
        if key not in self._analyses:  # the first writer wins
            await self.save_run(record.run, record.attempt)
            now = self._clock.now()
            for item in record.items:
                self._failures[item.failure.id] = item.failure
                self._diagnoses[item.failure.id] = [
                    StoredDiagnosis(
                        diagnosis,
                        now,
                        published,
                        item.usage if published else None,
                        cached_from=item.cached_from if published else None,
                    )
                    for diagnosis, published in item.diagnosis_rows()
                ]
            self._analyses[key] = list(record.items)
        stored = await self.run_analysis(*key)
        assert stored is not None
        return stored

    async def run_analysis(self, run_id: int, attempt: int) -> RunAnalysisRecord | None:
        items = self._analyses.get((run_id, attempt))
        if items is None:
            return None
        return RunAnalysisRecord(
            run=self._runs[run_id],
            attempt=self._attempts[(run_id, attempt)],
            # The failure is read again: ``resolve`` may have changed its status since.
            items=[
                item.model_copy(
                    update={
                        "failure": self._failures[item.failure.id],
                        "cached_from_run_id": self._origin_run(item.cached_from),
                    }
                )
                for item in items
            ],
        )

    def _origin_run(self, diagnosis_id: UUID | None) -> int | None:
        """The run of the ``diagnoses`` row with this id, like the join of the Postgres read."""
        if diagnosis_id is None:
            return None
        for failure_id, rows in self._diagnoses.items():
            if any(stored.id == diagnosis_id for stored in rows):
                return self._failures[failure_id].job.job.run.run_id
        return None

    def diagnosis_rows(self, failure_id: UUID) -> list[StoredDiagnosis]:
        """The ``diagnoses`` rows of a failure (tests, operations)."""
        return list(self._diagnoses.get(failure_id, []))

    async def cached_diagnosis(
        self, repo_id: int, signature_hash: str, max_age: timedelta, prompt_version: str
    ) -> CachedDiagnosis | None:
        oldest_allowed = self._clock.now() - max_age
        candidates = [
            stored
            for failure in self._failures.values()
            if _repo_id(failure) == repo_id
            and failure.signature.hash == signature_hash
            and not failure.job.job.run.is_fork
            for stored in self._diagnoses.get(failure.id, [])
            if stored.diagnosis.source == "llm"
            and stored.diagnosis.evidence_verified
            and stored.diagnosis.prompt_version == prompt_version
            and stored.created_at >= oldest_allowed
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda stored: stored.created_at)
        return CachedDiagnosis(diagnosis=newest.diagnosis, origin_id=newest.id)

    async def similar(
        self,
        repo_id: int,
        embedding: Sequence[float],
        k: int,
        min_score: float,
        exclude_run_id: int | None = None,
    ) -> list[SimilarFailure]:
        scored: list[SimilarFailure] = []
        for failure in self._failures.values():
            run = failure.job.job.run
            if (
                _repo_id(failure) != repo_id
                or failure.embedding is None
                or run.is_fork
                or run.run_id == exclude_run_id
            ):
                continue
            published = self._published_diagnosis(failure.id)
            if published is None:
                continue
            score = _cosine(embedding, failure.embedding)
            if score < min_score:
                continue
            scored.append(
                SimilarFailure(
                    failure_id=failure.id,
                    run_id=run.run_id,
                    run_attempt=run.run_attempt,
                    score=score,
                    category=published.category,
                    probable_cause=published.probable_cause,
                    fix_commit_sha=failure.fix_commit_sha,
                )
            )
        scored.sort(key=lambda item: (-item.score, str(item.failure_id)))
        return scored[:k]

    async def open_failures(self, repo_id: int, workflow_id: int, branch_key: str) -> list[Failure]:
        return [
            failure
            for failure in self._failures.values()
            if failure.status is FailureStatus.OPEN
            and _repo_id(failure) == repo_id
            and failure.job.job.run.workflow_id == workflow_id
            and failure.job.job.run.branch_key == branch_key
        ]

    async def resolve(
        self,
        failure_ids: Sequence[UUID],
        status: FailureStatus,
        fix_commit_sha: str | None,
    ) -> None:
        unknown = [failure_id for failure_id in failure_ids if failure_id not in self._failures]
        if unknown:  # all or nothing, like the Postgres transaction
            raise KeyError("unknown failure among " + ", ".join(map(str, unknown)))
        for failure_id in failure_ids:
            failure = self._failures[failure_id]
            self._failures[failure_id] = failure.model_copy(
                update={"status": status, "fix_commit_sha": fix_commit_sha}
            )

    async def publication(self, key: PublicationKey) -> Publication | None:
        return self._publications.get(key)

    async def save_publication(self, pub: Publication) -> bool:
        existing = self._publications.get(pub.key)
        if existing is not None and (existing.run_id, existing.attempt) > (pub.run_id, pub.attempt):
            return False
        self._publications[pub.key] = pub
        return True

    @asynccontextmanager
    async def publication_lock(self, key: PublicationKey) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with asyncio.timeout(self._lock_wait.total_seconds()):
                await lock.acquire()
        except TimeoutError as exc:
            raise TransientError(
                f"publication lock busy: waited {self._lock_wait.total_seconds():g} s"
            ) from exc
        try:
            yield
        finally:
            lock.release()

    def _published_diagnosis(self, failure_id: UUID) -> Diagnosis | None:
        return next(
            (item.diagnosis for item in self._diagnoses.get(failure_id, []) if item.published), None
        )


def _repo_id(failure: Failure) -> int:
    return failure.job.job.run.repo_id


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"embedding size mismatch: {len(a)} != {len(b)}")
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)
