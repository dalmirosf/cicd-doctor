"""Domain models (SPEC §5). Every model is an immutable Pydantic v2 model (D-03).

Models marked "P0 placeholder" are referenced by the ports in SPEC §4.3 but not defined in the
spec; their fields are the minimum the ports need and will be refined in P1.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class FailedJob(DomainModel):
    job: JobRef
    step_name: str | None
    step_number: int | None
    started_at: datetime
    completed_at: datetime
    runner_os: str | None


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
    number: int  # original line number
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
    fallback_reason: (
        Literal["llm_error", "invalid_output", "budget", "fork_policy", "config_off", "disabled"]
        | None
    ) = None
    evidence_verified: bool = True


class RuleMatch(DomainModel):
    rule_id: str
    category: Category
    confidence: float
    cause: str
    fix: str
    evidence_lines: list[int]


class SimilarFailure(DomainModel):
    failure_id: UUID
    run_url: str
    score: float
    category: Category
    probable_cause: str
    fix_commit_sha: str | None


class DiagnosisReport(DomainModel):
    run: RunRef
    items: list[tuple[Failure, Diagnosis]]
    similar: dict[UUID, list[SimilarFailure]] = Field(default_factory=dict)
    rules_baseline: dict[UUID, Diagnosis] = Field(default_factory=dict)  # shown on disagreement


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


# --- Queue, config and budget port types (P0 placeholders) --------------------------------


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
    conclusion: Literal["failure", "timed_out"]
    completed_at: datetime  # workflow_run.updated_at
    default_branch: str
    private: bool


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
