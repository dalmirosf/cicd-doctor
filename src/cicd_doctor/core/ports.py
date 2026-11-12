"""Ports: the only way ``core`` talks to the outside world (SPEC §4.3, D-02).

Adapters implement these Protocols; use cases receive them by injection.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from cicd_doctor.core.models import (
    CommentRef,
    CommentTarget,
    Delivery,
    Diagnosis,
    DiagnosisReport,
    DiagnosisRequest,
    FailedJob,
    Failure,
    FailureStatus,
    Job,
    JobRef,
    LLMResult,
    NewJob,
    Publication,
    PublicationKey,
    RawLog,
    RepoConfig,
    RepoRef,
    Reservation,
    RunAttempt,
    RunRef,
    SimilarFailure,
    Suggestion,
    WorkflowRun,
)


class LogSource(Protocol):
    async def failed_jobs(self, run: RunRef) -> list[FailedJob]: ...

    async def job_log(self, job: JobRef, max_bytes: int) -> RawLog:
        """Tail-biased download; the result is never persisted."""
        ...


class Embedder(Protocol):
    dims: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FailureStore(Protocol):
    async def save_run(self, run: WorkflowRun, attempt: RunAttempt) -> None: ...

    async def save_failure(self, failure: Failure) -> None: ...

    async def save_diagnosis(self, failure_id: UUID, diagnosis: Diagnosis) -> None: ...

    async def cached_diagnosis(
        self, repo_id: int, signature_hash: str, max_age: timedelta
    ) -> Diagnosis | None: ...

    async def similar(
        self, repo_id: int, embedding: Sequence[float], k: int, min_score: float
    ) -> list[SimilarFailure]: ...

    async def open_failures(
        self, repo_id: int, workflow_id: int, branch_key: str
    ) -> list[Failure]: ...

    async def resolve(
        self,
        failure_ids: Sequence[UUID],
        status: FailureStatus,
        fix_commit_sha: str | None,
    ) -> None: ...

    async def publication(self, key: PublicationKey) -> Publication | None: ...

    async def save_publication(self, pub: Publication) -> bool:
        """Return False (and store nothing) if a newer attempt already published."""
        ...


class LLMClient(Protocol):
    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        """Raw JSON + usage + latency; validation happens in core."""
        ...


class Reporter(Protocol):
    async def start_check(self, run: RunRef) -> int: ...

    async def complete_check(self, check_run_id: int, report: DiagnosisReport) -> None: ...

    async def upsert_comment(
        self, target: CommentTarget, body: str, known_id: int | None
    ) -> int: ...

    async def post_suggestion(self, target: CommentTarget, suggestion: Suggestion) -> None:
        """P3."""
        ...

    async def react(self, comment: CommentRef, reaction: str) -> None:
        """P3."""
        ...


class JobQueue(Protocol):
    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        """Return False on a duplicate delivery or dedupe key."""
        ...

    async def lease(self, worker_id: str, lease: timedelta) -> Job | None: ...

    async def complete(self, job_id: int) -> None: ...

    async def retry(self, job_id: int, run_after: datetime, error: str) -> None: ...

    async def fail(self, job_id: int, error: str, dead: bool) -> None: ...


class RepoConfigSource(Protocol):
    async def load(self, repo: RepoRef) -> RepoConfig:
        """Read from the default branch only; defaults if absent or invalid (D-26)."""
        ...


class UsageLedger(Protocol):
    async def reserve(self, installation_id: int, est_cost_usd: Decimal) -> Reservation | None:
        """Return None when a limit would be exceeded."""
        ...

    async def settle(self, reservation: Reservation, actual_cost_usd: Decimal) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
