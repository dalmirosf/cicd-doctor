"""In-memory ``FailureStore``: reference behaviour for the contract tests and offline runs."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.core.models import (
    Diagnosis,
    Failure,
    FailureStatus,
    Publication,
    PublicationKey,
    RunAttempt,
    SimilarFailure,
    WorkflowRun,
)
from cicd_doctor.core.ports import Clock


@dataclass(frozen=True)
class _StoredDiagnosis:
    diagnosis: Diagnosis
    created_at: datetime


class InMemoryFailureStore:
    """Implements ``FailureStore``. Every lookup is scoped by ``repo_id`` (D-10)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._runs: dict[int, WorkflowRun] = {}
        self._attempts: dict[tuple[int, int], RunAttempt] = {}
        self._failures: dict[UUID, Failure] = {}
        self._diagnoses: dict[UUID, list[_StoredDiagnosis]] = {}
        self._publications: dict[PublicationKey, Publication] = {}

    async def save_run(self, run: WorkflowRun, attempt: RunAttempt) -> None:
        self._runs[run.id] = run
        self._attempts[(attempt.run_id, attempt.attempt)] = attempt

    async def save_failure(self, failure: Failure) -> None:
        self._failures[failure.id] = failure

    async def save_diagnosis(self, failure_id: UUID, diagnosis: Diagnosis) -> None:
        if failure_id not in self._failures:
            raise KeyError(f"unknown failure {failure_id}")
        stored = _StoredDiagnosis(diagnosis, self._clock.now())
        self._diagnoses.setdefault(failure_id, []).append(stored)

    async def cached_diagnosis(
        self, repo_id: int, signature_hash: str, max_age: timedelta
    ) -> Diagnosis | None:
        oldest_allowed = self._clock.now() - max_age
        candidates = [
            stored
            for failure in self._failures.values()
            if _repo_id(failure) == repo_id and failure.signature.hash == signature_hash
            for stored in self._diagnoses.get(failure.id, [])
            if stored.diagnosis.source == "llm" and stored.created_at >= oldest_allowed
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda stored: stored.created_at).diagnosis

    async def similar(
        self, repo_id: int, embedding: Sequence[float], k: int, min_score: float
    ) -> list[SimilarFailure]:
        scored: list[SimilarFailure] = []
        for failure in self._failures.values():
            if _repo_id(failure) != repo_id or failure.embedding is None:
                continue
            latest = self._latest_diagnosis(failure.id)
            if latest is None:
                continue
            score = _cosine(embedding, failure.embedding)
            if score < min_score:
                continue
            run = failure.job.job.run
            scored.append(
                SimilarFailure(
                    failure_id=failure.id,
                    run_url=f"https://github.com/{run.repo_full_name}/actions/runs/{run.run_id}",
                    score=score,
                    category=latest.category,
                    probable_cause=latest.probable_cause,
                    fix_commit_sha=failure.fix_commit_sha,
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
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

    def _latest_diagnosis(self, failure_id: UUID) -> Diagnosis | None:
        stored = self._diagnoses.get(failure_id)
        if not stored:
            return None
        return max(stored, key=lambda item: item.created_at).diagnosis


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
