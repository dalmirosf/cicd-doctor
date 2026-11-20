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
    CachedDiagnosis,
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
    LoadedConfig,
    NewJob,
    Publication,
    PublicationKey,
    PublishedUpgrade,
    PullDiff,
    PullRequestInfo,
    PurgeCounts,
    RawLog,
    RepoRef,
    Repository,
    Reservation,
    ResolvedBy,
    ReviewSuggestion,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    RunSummary,
    SimilarFailure,
    SuccessRef,
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

    async def upgrade_published(
        self, run_id: int, attempt: int, upgrades: Sequence[PublishedUpgrade]
    ) -> RunAnalysisRecord:
        """Replace the published rules diagnosis of some failures of an analysed attempt by an
        ``llm`` or ``cache`` one and return the stored record (D-95).

        One transaction for all of them: per failure the published row becomes a plain,
        unpublished rules row (its usage stays on it, so ``SUM(cost_usd)`` is the real spend) and
        the new row is published with its usage. A failure whose published diagnosis is no longer
        a rules one is left alone, so a repeated or concurrent call changes nothing. ``KeyError``
        for an attempt that was never analysed or a failure that is not one of its failures
        (nothing is written then).
        """
        ...

    async def cached_diagnosis(
        self, repo_id: int, signature_hash: str, max_age: timedelta, prompt_version: str
    ) -> CachedDiagnosis | None:
        """The newest LLM diagnosis of the same signature in this repository that is still
        within ``max_age``, was verified, was made with ``prompt_version`` and comes from a run
        that is not a fork (D-81)."""
        ...

    async def similar(
        self,
        repo_id: int,
        embedding: Sequence[float],
        k: int,
        min_score: float,
        exclude_run_id: int | None = None,
    ) -> list[SimilarFailure]:
        """Up to ``k`` past failures of this repository, best first, with a score of at least
        ``min_score`` (cosine similarity), published diagnosis only, never from a fork and never
        from ``exclude_run_id`` (D-79)."""
        ...

    async def open_failures(
        self, repo_id: int, workflow_id: int, branch_key: str
    ) -> list[Failure]: ...

    async def record_success(self, run: WorkflowRun, attempt: RunAttempt) -> None:
        """Record a successful attempt (D-83): the run only if it has no row (so the
        ``pr_number`` a diagnosis found is never overwritten) and the attempt with its
        ``success`` conclusion. Idempotent; creates no failure. The repository must be known."""
        ...

    async def later_successes(
        self,
        repo_id: int,
        workflow_id: int,
        branch_key: str,
        after_run_id: int,
        after_attempt: int,
    ) -> list[SuccessRef]:
        """The recorded successes of this workflow and branch key that come after
        ``(after_run_id, after_attempt)``, ordered by ``(run_id, attempt)`` (D-83)."""
        ...

    async def resolve(
        self,
        failure_ids: Sequence[UUID],
        status: FailureStatus,
        fix_commit_sha: str | None,
        resolved_by: ResolvedBy | None = None,
    ) -> None:
        """Set the status of the failures, all or nothing (``KeyError`` for an unknown id).
        ``resolved_by`` is the success that resolved them (D-83, D-84)."""
        ...

    async def resolved_by(self, repo_id: int, run_id: int, attempt: int) -> list[Failure]:
        """The failures of this repository that the success ``(run_id, attempt)`` resolved."""
        ...

    async def mark_stale(self, before: datetime, now: datetime) -> int:
        """Mark as ``stale`` every ``open`` failure created before ``before``; the resolved ones
        are never touched. Returns how many changed (D-85)."""
        ...

    async def purge(self, attempts_before: datetime, publications_before: datetime) -> PurgeCounts:
        """Delete the run attempts completed before ``attempts_before`` with their failures and
        diagnoses, the runs that are left without attempts, and the publications last updated
        before ``publications_before``. In batches, one transaction each (D-85)."""
        ...

    async def purge_repositories(self, repo_ids: Sequence[int]) -> PurgeCounts:
        """Delete every run, attempt, failure, diagnosis and publication of these repositories
        (D-34, D-85). The repository rows stay."""
        ...

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

    async def post_suggestion(self, target: CommentTarget, suggestion: ReviewSuggestion) -> bool:
        """Post the suggestion as a review comment on the pull request (D-97), or edit the one
        this app already wrote for its marker (the first line of ``suggestion.body``), found
        among the comments of ``<slug>[bot]`` only. ``True`` when it is there; ``False`` when
        GitHub refuses the range (422: a line outside the diff, a moved head, spam), which is
        "text only", not an error. A missing permission raises ``PermanentError``, a 5xx or a
        rate limit ``TransientError``. The caller holds the publication lock."""
        ...

    async def react(self, comment: CommentRef, reaction: str) -> None:
        """Add ``reaction`` (``eyes``, ``confused``, ...) to an issue or PR comment, with the
        permissions of ``explain`` (D-89). Adding one that is already there is a success. A
        deleted comment raises ``CommentGone``; a missing permission raises ``PermanentError``."""
        ...


class PullRequestSource(Protocol):
    async def find(self, run: RunRef) -> int | None:
        """The number of the open pull request this run belongs to, or None (D-72)."""
        ...

    async def get(
        self, repo: RepoRef, number: int, *, for_explain: bool = True
    ) -> PullRequestInfo | None:
        """The pull request, or None when it does not exist (404). Read with the permissions of
        ``explain`` (D-89) or, with ``for_explain=False``, the ones of the diagnosis: the
        automatic flow must not need Issues: write. Transient errors raise ``TransientError``;
        a missing permission raises ``PermanentError``."""
        ...

    async def diff(self, repo: RepoRef, number: int) -> PullDiff:
        """The files of the pull request with their ``patch`` (``GET /pulls/{n}/files``, up to
        30 pages of 100), read with the permissions of the diagnosis (D-97). ``truncated`` is
        true when the cap left files out. A 404 gives an empty diff. Transient errors raise
        ``TransientError``."""
        ...


class RunSource(Protocol):
    async def runs_for_head(self, repo: RepoRef, head_sha: str) -> list[RunSummary]:
        """The workflow runs of one head commit, newest first, one page of at most 100 (D-96).
        Read with the permissions of ``explain``. Items that GitHub shapes in an unexpected way
        are skipped. Transient errors raise ``TransientError``."""
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

    async def purge(
        self, deliveries_before: datetime, finished_jobs_before: datetime
    ) -> PurgeCounts:
        """Delete the deliveries received before ``deliveries_before`` and the ``done``,
        ``failed`` and ``dead`` jobs last updated before ``finished_jobs_before``; queued and
        running jobs are never touched. In batches, one transaction each (D-85)."""
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

    async def gone_repositories(self, deleted_before: datetime) -> list[int]:
        """The ids of the repositories removed from an installation, or whose installation was
        deleted, before ``deleted_before`` (D-34, D-85)."""
        ...

    async def repository(self, repo_id: int) -> Repository | None: ...


class RepoConfigSource(Protocol):
    async def load(self, repo: RepoRef) -> LoadedConfig:
        """Read from the default branch only, never from another ref (D-90). A missing file is
        ``default``; an invalid one is ``invalid`` (the caller turns the LLM off); a transient
        error raises ``TransientError`` and is never turned into the defaults."""
        ...


class CommandLog(Protocol):
    """The accepted ``explain`` commands, for the limit of one per PR per window (D-93)."""

    async def claim(
        self,
        repo_id: int,
        pr_number: int,
        comment_id: int,
        actor_login: str,
        now: datetime,
        window: timedelta,
    ) -> bool:
        """Decide and record, atomically per ``(repo_id, pr_number)``: ``True`` when this comment
        already holds a claim (a retry of the same command) or no other claim of the PR is
        younger than ``window``, in which case it is recorded at ``now``; ``False`` otherwise,
        recording nothing (a refused command never extends the window)."""
        ...

    async def purge(self, before: datetime) -> PurgeCounts:
        """Delete the claims made before ``before`` (retention, D-85)."""
        ...


class UsageLedger(Protocol):
    async def reserve(self, installation_id: int, est_cost_usd: Decimal) -> Reservation | None:
        """Return None when a limit would be exceeded."""
        ...

    async def settle(self, reservation: Reservation, actual_cost_usd: Decimal) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
