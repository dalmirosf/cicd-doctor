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
    Installation,
    Job,
    JobRef,
    LLMResult,
    NewJob,
    Publication,
    PublicationKey,
    RawLog,
    RepoConfig,
    RepoRef,
    Repository,
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
    """At-least-once job queue (D-05, D-47).

    Every ``lease`` increments ``Job.attempts``, so ``(job.id, job.attempts)`` identifies one
    lease. The methods that take a leased ``Job`` change nothing and return False when that lease
    is no longer the current one (it expired and another worker took the job, or it finished).
    """

    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        """Return False on a duplicate delivery or dedupe key."""
        ...

    async def lease(self, worker_id: str, lease: timedelta) -> Job | None:
        """Lease the next ready job, or return None. Exhausted jobs become ``dead`` instead."""
        ...

    async def heartbeat(self, job: Job, lease: timedelta) -> bool:
        """Extend the lease to ``now + lease`` while the job is still running."""
        ...

    async def complete(self, job: Job) -> bool: ...

    async def retry(self, job: Job, run_after: datetime, error: str) -> bool: ...

    async def fail(self, job: Job, error: str, dead: bool) -> bool:
        """``dead=False``: permanent error (``failed``); ``dead=True``: attempts exhausted."""
        ...

    async def wait_for_jobs(self, max_wait: timedelta) -> bool:
        """Return True when new jobs may be ready (for example after an enqueue), or False
        after ``max_wait``. Callers still poll: a wake-up is a hint, not a guarantee."""
        ...


class InstallationStore(Protocol):
    """Installations and repositories tracked from webhooks (FR-03, D-54). Upserts only."""

    async def save_installation(self, installation: Installation) -> None: ...

    async def save_repositories(self, repositories: Sequence[Repository]) -> None:
        """Upsert. A ``None`` ``default_branch`` keeps the stored one."""
        ...

    async def remove_repositories(self, repo_ids: Sequence[int], removed_at: datetime) -> None: ...

    async def installation(self, installation_id: int) -> Installation | None: ...

    async def repository(self, repo_id: int) -> Repository | None: ...


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
