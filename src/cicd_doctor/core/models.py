"""Domain models (SPEC §5). Every model is an immutable Pydantic v2 model (D-03).

Models marked "P0 placeholder" are referenced by the ports in SPEC §4.3 but not defined in the
spec; their fields are the minimum the ports need and will be refined in P1.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- Enums --------------------------------------------------------------------------------


class Category(StrEnum):
    BROKEN_TEST = "broken_test"  # failing tests and code-quality gates (lint, types), D-13
    DEPENDENCY = "dependency"
    NETWORK = "network"  # DNS, connection resets, registry/API rate limits
    CONFIG_OR_SECRETS = "config_or_secrets"
    TIMEOUT = "timeout"
    INFRA_OR_DEPLOY = "infra_or_deploy"  # runner lost, OOM, cloud/deploy errors
    FLAKY = "flaky"  # *suspected* flaky; confirmed only by FailureStatus (D-15)
    UNKNOWN = "unknown"


# Why a rules diagnosis was published instead of an LLM one (SPEC §5, §8, §9.4).
FallbackReason = Literal[
    "llm_error", "invalid_output", "budget", "fork_policy", "config_off", "disabled"
]


class FailureStatus(StrEnum):
    OPEN = "open"
    RESOLVED_FLAKY = "resolved_flaky"
    RESOLVED_BY_COMMIT = "resolved_by_commit"
    STALE = "stale"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class JobKind(StrEnum):
    DIAGNOSE_RUN = "diagnose_run"
    RECORD_OUTCOME = "record_outcome"
    EXPLAIN = "explain"
    PURGE = "purge"
    SYNC_INSTALLATION = "sync_installation"  # installation / installation_repositories (D-54)


class InstallationAction(StrEnum):
    """What an ``installation`` or ``installation_repositories`` webhook did (FR-03)."""

    CREATED = "created"
    DELETED = "deleted"
    SUSPENDED = "suspended"
    UNSUSPENDED = "unsuspended"
    REPOSITORIES_ADDED = "repositories_added"
    REPOSITORIES_REMOVED = "repositories_removed"


class PublicationKind(StrEnum):
    CHECK = "check"
    COMMENT = "comment"


# --- GitHub run identity ------------------------------------------------------------------


class RunRef(DomainModel):
    """Identifies one attempt of a workflow run."""

    installation_id: int
    repo_id: int
    repo_full_name: str
    run_id: int
    run_attempt: int
    workflow_id: int
    workflow_name: str
    head_sha: str
    head_branch: str | None
    head_repo_full_name: str
    event: str
    is_fork: bool
    pr_number: int | None

    @property
    def branch_key(self) -> str:
        """Identifies a branch across forks: head repo + head branch (SPEC §7.2, D-24)."""
        return f"{self.head_repo_full_name}:{self.head_branch or ''}"


class JobRef(DomainModel):
    run: RunRef
    job_id: int
    job_name: str


class WorkflowStep(DomainModel):
    """One step of a job, normalised from the GitHub jobs API (D-58)."""

    number: int
    name: str
    conclusion: str | None  # success, failure, cancelled, skipped, timed_out; None while running
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowJob(DomainModel):
    """One job of a run attempt, normalised from the GitHub jobs API (D-58).

    ``LogSource.jobs`` returns every job of the attempt; ``core/logs`` picks the failed ones.
    """

    job_id: int
    name: str
    conclusion: str | None  # success, failure, cancelled, skipped, timed_out; None while running
    started_at: datetime | None
    completed_at: datetime | None
    runner_os: str | None  # "Linux" | "Windows" | "macOS", derived from the runner labels
    steps: list[WorkflowStep] = Field(default_factory=list)


class FailedJob(DomainModel):
    job: JobRef
    conclusion: Literal["failure", "timed_out"]
    step_name: str | None
    step_number: int | None
    started_at: datetime
    completed_at: datetime
    runner_os: str | None
    # Time range of the failed step, used to slice its lines out of the job log (SPEC §7.3).
    step_started_at: datetime | None = None
    step_completed_at: datetime | None = None


class WorkflowRun(DomainModel):
    """P0 placeholder: run-level data persisted once per run (table ``workflow_runs``)."""

    id: int
    repo_id: int
    workflow_id: int
    workflow_name: str
    head_sha: str
    head_branch: str | None
    head_repo_full_name: str
    event: str
    is_fork: bool
    pr_number: int | None


class RunAttempt(DomainModel):
    """P0 placeholder: one attempt of a run (table ``run_attempts``)."""

    run_id: int
    attempt: int
    conclusion: str
    completed_at: datetime


class RawLog(DomainModel):
    """A job log as downloaded. Lives only in worker memory and is never persisted (D-12)."""

    text: str = Field(repr=False)  # repr=False: keep raw logs out of accidental log lines
    truncated: bool


# --- Log excerpt and signature ------------------------------------------------------------


class LogLine(DomainModel):
    number: int  # 1-based line number in the retained log (D-58: relative to the tail if truncated)
    text: str  # cleaned + redacted text


class LogExcerpt(DomainModel):
    job: JobRef
    step_name: str | None
    lines: list[LogLine]  # ordered; gaps are explicit via `omitted`
    omitted: list[tuple[int, int]]  # (from_line, to_line) ranges dropped
    error_lines: list[int]  # line numbers that matched error patterns
    token_count: int
    truncated: bool  # the log exceeded LOG_MAX_BYTES
    redactions: int  # count only, never the values


class Signature(DomainModel):
    text: str
    hash: str  # sha256 hex of text


class Failure(DomainModel):
    id: UUID
    job: FailedJob
    excerpt: LogExcerpt
    signature: Signature
    embedding: list[float] | None
    status: FailureStatus
    fix_commit_sha: str | None


# A fixed namespace: the same job of the same attempt gets the same failure id in every adapter
# and in every retry (D-69). It is not a secret.
FAILURE_ID_NAMESPACE = UUID("c3a1f0d2-6b7e-5c48-9d1a-2e4f8b6a7c90")


def failure_id_for(run_id: int, attempt: int, job_id: int) -> UUID:
    """The id of the failure of ``job_id`` in ``attempt`` of ``run_id`` (UUIDv5, D-69)."""
    return uuid5(FAILURE_ID_NAMESPACE, f"{run_id}:{attempt}:{job_id}")


# --- Diagnosis ----------------------------------------------------------------------------


class Evidence(DomainModel):
    line: int
    text: str = Field(max_length=300)


class Suggestion(DomainModel):
    path: str
    start_line: int
    end_line: int
    replacement: str = Field(max_length=2000)


class Diagnosis(DomainModel):
    category: Category
    probable_cause: str = Field(max_length=500)
    evidence: list[Evidence] = Field(min_length=0, max_length=8)
    suggested_fix: str = Field(max_length=1000)
    suggestion: Suggestion | None = None
    confidence: float = Field(ge=0, le=1)
    # metadata (not produced by the LLM)
    source: Literal["rules", "llm", "cache"]
    rule_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    fallback_reason: FallbackReason | None = None
    evidence_verified: bool = True


class RuleMatch(DomainModel):
    rule_id: str
    category: Category
    confidence: float
    cause: str
    fix: str
    evidence_lines: list[int]


class SimilarFailure(DomainModel):
    """A past failure of the same repository that looks like the current one (D-79, D-82).

    It carries ids, not a URL: the renderer builds the link, so no stored string is trusted as
    one (D-64). The score is rounded so that the same inputs print the same on every platform.
    """

    failure_id: UUID
    run_id: int = Field(ge=0)
    run_attempt: int = Field(ge=1)
    score: float
    category: Category
    probable_cause: str
    fix_commit_sha: str | None

    @field_validator("score")
    @classmethod
    def _round_score(cls, value: float) -> float:
        return round(value, 3)


class SuccessRef(DomainModel):
    """A recorded successful attempt of a workflow run (D-83): what the late resolution reads."""

    run_id: int
    attempt: int = Field(ge=1)
    head_sha: str


class ResolvedBy(DomainModel):
    """The successful attempt that resolved a failure (``failures.resolved_by_*``, D-83, D-84)."""

    run_id: int
    attempt: int = Field(ge=1)


class CachedDiagnosis(DomainModel):
    """An earlier LLM diagnosis served from the cache, with the ``diagnoses`` row it came from
    (stored in ``cached_from``, D-81)."""

    diagnosis: Diagnosis
    origin_id: UUID


class DiagnosisReport(DomainModel):
    run: RunRef
    items: list[tuple[Failure, Diagnosis]]
    similar: dict[UUID, list[SimilarFailure]] = Field(default_factory=dict)
    rules_baseline: dict[UUID, Diagnosis] = Field(default_factory=dict)  # shown on disagreement
    # failure id -> the run a ``cache`` diagnosis was served from (an int: the renderer builds text)
    cached_from_runs: dict[UUID, int] = Field(default_factory=dict)


# --- What a run attempt leaves behind: the analysis checkpoint (D-69) ----------------------


class DiagnosisUsage(DomainModel):
    """What the LLM calls of one job used. It lives on the published ``diagnoses`` row, also when
    the published diagnosis is the rules fallback of a job whose calls failed, so
    ``SUM(cost_usd)`` is the real spend."""

    model: str | None = None  # of the last answer; None when no call answered
    calls: int = 0  # calls that answered (they are billed)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal(0)
    failed_calls: int = 0  # attempts that raised, retried or not: they cost nothing


class StoredFailure(DomainModel):
    """A failure with the diagnoses of its job: the one that is published and the rules one."""

    failure: Failure
    published: Diagnosis  # what the check run and the comment show
    rules: Diagnosis  # always computed (D-35)
    usage: DiagnosisUsage | None = None  # None when no LLM call was made
    # The ``diagnoses.id`` a published ``cache`` diagnosis was copied from (``cached_from``, D-81).
    cached_from: UUID | None = None
    # The run of that row, filled by the store when it reads the record back (never written).
    cached_from_run_id: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_rows(self) -> Self:
        """The invariants that let the store keep a diagnosis as one row per source (D-69)."""
        published, rules = self.published, self.rules
        if published.source != "cache" and self.cached_from is not None:
            raise ValueError("only a cached diagnosis has an origin")
        if rules.source != "rules" or rules.model is not None or rules.fallback_reason is not None:
            raise ValueError("the rules diagnosis must be a plain rules diagnosis")
        if published.source == "rules":
            if (
                published.model is not None
                or published.model_copy(update={"fallback_reason": None}) != rules
            ):
                raise ValueError("a published rules diagnosis is the rules one plus a reason")
        elif published.source == "cache" and self.usage is not None:
            raise ValueError("a cached diagnosis made no LLM call")
        elif (
            published.source == "llm"
            and self.usage is not None
            and self.usage.model != published.model
        ):
            raise ValueError("the usage of an LLM diagnosis names the model that produced it")
        return self

    def diagnosis_rows(self) -> list[tuple[Diagnosis, bool]]:
        """The diagnoses to keep, one per source, each with whether it is the published one.

        The rules diagnosis is always kept. The LLM (or cache) diagnosis is kept when it is the
        published one; a published rules fallback is the rules row itself, and exactly one row is
        published (D-69). The usage belongs to the published row.
        """
        if self.published.source == "rules":
            return [(self.published, True)]
        return [(self.rules, False), (self.published, True)]


class RunAnalysisRecord(DomainModel):
    """The analysis of one run attempt: the checkpoint that makes publishing repeatable (D-69).

    ``items`` is ordered by job id, whatever the order it was built in, so a record that goes
    through any adapter and comes back is equal to itself. A failure-less attempt (items empty)
    is a valid checkpoint.
    """

    run: WorkflowRun
    attempt: RunAttempt
    items: list[StoredFailure] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _sort_items(cls, items: list[StoredFailure]) -> list[StoredFailure]:
        return sorted(items, key=lambda item: item.failure.job.job.job_id)

    @model_validator(mode="after")
    def _check_scope(self) -> Self:
        if self.attempt.run_id != self.run.id:
            raise ValueError("the attempt belongs to another run")
        seen: set[int] = set()
        for item in self.items:
            failure_run = item.failure.job.job.run
            job_id = item.failure.job.job.job_id
            if (
                failure_run.run_id != self.run.id
                or failure_run.run_attempt != self.attempt.attempt
                or failure_run.repo_id != self.run.repo_id
            ):
                raise ValueError(f"the failure of job {job_id} belongs to another run attempt")
            if item.failure.id != failure_id_for(self.run.id, self.attempt.attempt, job_id):
                raise ValueError(f"the failure id of job {job_id} is not failure_id_for(...)")
            if job_id in seen:
                raise ValueError(f"job {job_id} appears twice")
            seen.add(job_id)
        return self


# --- LLM port types (P0 placeholders) -----------------------------------------------------


class DiagnosisRequest(DomainModel):
    job: FailedJob
    excerpt: LogExcerpt
    rule_hints: list[RuleMatch] = Field(default_factory=list)
    similar: list[SimilarFailure] = Field(default_factory=list)
    repair_error: str | None = None  # validation error sent back on the single repair attempt


class LLMResult(DomainModel):
    raw_json: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


# --- Reporter port types (P0 placeholders) ------------------------------------------------


class CheckOutput(DomainModel):
    """The title and the markdown summary of a check run (already rendered and safe)."""

    title: str
    summary: str


class CommentTarget(DomainModel):
    run: RunRef
    pr_number: int


class CommentRef(DomainModel):
    installation_id: int
    repo_full_name: str
    comment_id: int


class PublicationKey(DomainModel):
    """Identifies an idempotent output: one check run per run, one comment per (PR, workflow)."""

    kind: PublicationKind
    repo_id: int
    run_id: int | None = None  # CHECK only
    pr_number: int | None = None  # COMMENT only
    workflow_id: int | None = None  # COMMENT only

    @model_validator(mode="after")
    def _check_scope(self) -> Self:
        if self.kind is PublicationKind.CHECK:
            valid = self.run_id is not None and self.pr_number is None and self.workflow_id is None
        else:
            valid = (
                self.run_id is None and self.pr_number is not None and self.workflow_id is not None
            )
        if not valid:
            raise ValueError(f"fields do not match publication kind {self.kind.value!r}")
        return self

    @classmethod
    def check(cls, repo_id: int, run_id: int) -> Self:
        return cls(kind=PublicationKind.CHECK, repo_id=repo_id, run_id=run_id)

    @classmethod
    def comment(cls, repo_id: int, pr_number: int, workflow_id: int) -> Self:
        return cls(
            kind=PublicationKind.COMMENT,
            repo_id=repo_id,
            pr_number=pr_number,
            workflow_id=workflow_id,
        )


class Publication(DomainModel):
    """What was published for a key, and by which (run, attempt). Newer attempts win."""

    key: PublicationKey
    external_id: int  # GitHub check_run_id or comment_id
    run_id: int
    attempt: int

    @model_validator(mode="after")
    def _check_run(self) -> Self:
        if self.key.kind is PublicationKind.CHECK and self.run_id != self.key.run_id:
            raise ValueError("a check publication belongs to the run of its key")
        return self


# --- Queue, config and budget port types (P0 placeholders) --------------------------------


class PurgeCounts(DomainModel):
    """Rows removed (or changed) by a retention step, by table. Only counts: a purge never reports
    content (D-85)."""

    deleted: dict[str, int] = Field(default_factory=dict)

    def __add__(self, other: "PurgeCounts") -> "PurgeCounts":
        merged = dict(self.deleted)
        for table, count in other.deleted.items():
            merged[table] = merged.get(table, 0) + count
        return PurgeCounts(deleted=merged)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


class Delivery(DomainModel):
    delivery_id: UUID  # X-GitHub-Delivery
    event: str
    action: str | None
    installation_id: int | None


class NewJob(DomainModel):
    kind: JobKind
    payload: dict[str, Any]
    dedupe_key: str | None = None
    run_after: datetime | None = None
    max_attempts: int = 5


def diagnose_dedupe_key(run_id: int, run_attempt: int) -> str:
    """One ``diagnose_run`` job per run attempt, whatever the delivery (D-06, FR-04)."""
    return f"diagnose:{run_id}:{run_attempt}"


class Job(DomainModel):
    id: int
    kind: JobKind
    payload: dict[str, Any]
    dedupe_key: str | None
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_until: datetime | None
    locked_by: str | None
    last_error: str | None


class DiagnoseRunPayload(DomainModel):
    """Payload of a ``diagnose_run`` job: what the webhook knew, never the raw GitHub JSON."""

    run: RunRef
    # ``cancelled`` runs are examined only to find a job-level timeout (D-73).
    conclusion: Literal["failure", "timed_out", "cancelled"]
    completed_at: datetime  # workflow_run.updated_at
    default_branch: str
    private: bool
    # The account of the installation (``repository.owner``, D-75). Optional because the jobs
    # queued before P1c carry no account.
    account_login: str | None = None
    account_type: str | None = None


# --- Installations (FR-03, D-54) ----------------------------------------------------------


class Installation(DomainModel):
    id: int  # GitHub installation id
    account_login: str
    account_type: str  # "User" | "Organization"
    suspended_at: datetime | None = None
    deleted_at: datetime | None = None


class Repository(DomainModel):
    id: int  # GitHub repository id
    installation_id: int
    full_name: str
    private: bool
    default_branch: str | None = None  # installation events do not carry it (D-54)
    removed_at: datetime | None = None


class InstallationEvent(DomainModel):
    """Payload of a ``sync_installation`` job, normalised from the webhook."""

    action: InstallationAction
    installation: Installation  # account data only; the use case decides the timestamps
    repositories: list[Repository] = Field(default_factory=list)  # created or added
    removed_repository_ids: list[int] = Field(default_factory=list)
    occurred_at: datetime  # when the webhook was received


class RepoRef(DomainModel):
    installation_id: int
    repo_id: int
    full_name: str
    default_branch: str


class RepoConfig(DomainModel):
    """Per-repo settings from ``.github/cicd-doctor.yml`` on the default branch (D-26)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment: bool = True
    llm: Literal["auto", "on_demand", "off"] = "auto"
    ignore_workflows: list[str] = Field(default_factory=list)


class Reservation(DomainModel):
    installation_id: int
    day: date  # UTC
    est_cost_usd: Decimal
