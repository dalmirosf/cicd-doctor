"""Ports: the only way ``core`` talks to the outside world (SPEC §4.3, D-02).

Adapters implement these Protocols; use cases receive them by injection.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from cicd_doctor.core.models import (
    CheckOutput,
    CommentRef,
    CommentTarget,
    Delivery,
    Diagnosis,
    DiagnosisRequest,
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
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    SimilarFailure,
    Suggestion,
    WorkflowJob,
    WorkflowRun,
)


class LogSource(Protocol):
    async def jobs(self, run: RunRef) -> list[WorkflowJob]:
        """Every job of the run attempt; ``core.logs.select_failed_jobs`` picks the failed ones
        (D-58)."""
        ...

    async def job_annotations(self, job: JobRef) -> list[str]:
        """The ``message`` of every annotation of the job's check run (the job id is the check
        run id). A job that hits its ``timeout-minutes`` ends ``cancelled`` and only its
        annotations say so (D-73). Transient errors (5xx, rate limits) raise ``TransientError``;
        403 and 404 raise ``PermanentError``."""
        ...

    async def job_log(self, job: JobRef, max_bytes: int) -> RawLog:
        """Tail-biased download of the last ``max_bytes`` of a job log; never persisted.

        ``RawLog.truncated`` is true whenever bytes were dropped. Raises ``LogsUnavailable``
        (a ``PermanentError`` that keeps ``status_code``: 404 or 410) when GitHub no longer has
        the log, and ``TransientError`` for network errors, 5xx, rate limits and an expired
        download URL (a retry asks for a new one, D-71)."""
        ...


class TokenCounter(Protocol):
    """Counts LLM tokens. tiktoken lives in an adapter because it downloads its encoding on
    first use (D-57)."""

    def count(self, text: str) -> int: ...


class Embedder(Protocol):
    dims: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FailureStore(Protocol):
    """Everything the diagnosis flow persists. Every lookup is scoped by repository (D-10)."""

    async def save_run(self, run: WorkflowRun, attempt: RunAttempt) -> None:
        """Upsert the run and the attempt; the run and its repository must be known."""
        ...

    async def save_failure(self, failure: Failure) -> None:
        """Upsert one failure (the attempt must have been saved)."""
        ...

    async def save_diagnosis(self, failure_id: UUID, diagnosis: Diagnosis) -> None:
        """Add one unpublished diagnosis row; ``KeyError`` for an unknown failure."""
        ...

    async def save_run_analysis(self, record: RunAnalysisRecord) -> RunAnalysisRecord:
        """Store the analysis of a run attempt atomically: the run, the attempt, the failures and
        their diagnoses in one transaction, which marks the attempt as analysed (D-69).

        The first writer wins: when the attempt is already analysed nothing changes and the
        stored record is returned, so every caller continues with the same record.
        """
        ...

    async def run_analysis(self, run_id: int, attempt: int) -> RunAnalysisRecord | None:
        """The stored analysis of an attempt, or None if it was never analysed. An attempt
        without failures is a valid checkpoint."""
        ...

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
        """Return False (and store nothing) if a newer attempt already published.

        "Newer" is ordered by ``(run_id, attempt)`` (the run id is the same for a check run).
        """
        ...

    def publication_lock(self, key: PublicationKey) -> AbstractAsyncContextManager[None]:
        """Serialise the writers of one output across workers and runs (D-70): the second owner
        waits for the first. Waiting more than the adapter's limit (30 s) raises
        ``TransientError``. The lock is released on exit, or when its connection dies."""
        ...


class LLMClient(Protocol):
    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        """Raw JSON + usage + latency; validation happens in core."""
        ...


class Reporter(Protocol):
    """Writes the check run and the sticky comment (D-70). The caller holds the publication lock
    and keeps the ids; an id it passes is a hint that may be stale (a 404 falls back to a search).
    """

    async def start_check(self, run: RunRef, known_id: int | None) -> int:
        """Put the check run of this run in progress and return its id: the known one, else the
        one found by ``external_id``, else a new one."""
        ...

    async def complete_check(self, run: RunRef, check_run_id: int, output: CheckOutput) -> None:
        """Complete the check run as ``neutral`` with ``output`` (D-18)."""
        ...

    async def upsert_comment(self, target: CommentTarget, body: str, known_id: int | None) -> int:
        """Update the sticky comment and return its id: the known one, else the bot's comment
        with the marker of ``body``, else a new one. Never touches a comment the bot did not
        write."""
        ...

    async def post_suggestion(self, target: CommentTarget, suggestion: Suggestion) -> None:
        """P3."""
        ...

    async def react(self, comment: CommentRef, reaction: str) -> None:
        """P3."""
        ...


class PullRequestSource(Protocol):
    async def find(self, run: RunRef) -> int | None:
        """The number of the open pull request this run belongs to, or None (D-72)."""
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

    async def ensure_installation(self, installation: Installation) -> None:
        """Insert the installation if it has no row; change nothing otherwise, so a stored
        ``suspended_at`` or ``deleted_at`` is never overwritten (D-75)."""
        ...

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
