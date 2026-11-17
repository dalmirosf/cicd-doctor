"""Inbound GitHub webhooks: signature check and translation into jobs (FR-01, FR-03, FR-04).

Only the fields CICD Doctor needs are parsed. The jobs carry normalised payloads
(``DiagnoseRunPayload``, ``InstallationEvent``), never the raw GitHub JSON (D-56).
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from cicd_doctor.core.models import (
    DiagnoseRunPayload,
    Installation,
    InstallationAction,
    InstallationEvent,
    JobKind,
    NewJob,
    RecordOutcomePayload,
    Repository,
    RunRef,
    diagnose_dedupe_key,
    outcome_dedupe_key,
)

SIGNATURE_PREFIX = "sha256="
# A ``success`` becomes a ``record_outcome`` job (FR-17, D-83); ``skipped``, ``neutral``,
# ``action_required`` and ``stale`` change nothing and answer 204.
# ``cancelled`` runs are enqueued too: a job that exceeds its ``timeout-minutes`` ends the run
# ``cancelled``, and the use case decides whether it was a timeout (D-73).
DIAGNOSED_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out", "cancelled"})

_INSTALLATION_ACTIONS = {
    "created": InstallationAction.CREATED,
    "deleted": InstallationAction.DELETED,
    "suspend": InstallationAction.SUSPENDED,
    "unsuspend": InstallationAction.UNSUSPENDED,
}
_REPOSITORIES_ACTIONS = {
    "added": InstallationAction.REPOSITORIES_ADDED,
    "removed": InstallationAction.REPOSITORIES_REMOVED,
}


class InvalidPayloadError(ValueError):
    """An event we handle lacks a field we need. The message names fields, never values."""


def verify_signature(body: bytes, header: str | None, secrets: Sequence[bytes]) -> bool:
    """Check ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body) against every secret.

    Every secret is compared, without stopping at the first match, so the timing does not tell
    which one (current or previous) matched.
    """
    if not header or not header.startswith(SIGNATURE_PREFIX) or not secrets:
        return False
    try:
        received = header.removeprefix(SIGNATURE_PREFIX).strip().encode("ascii")
    except UnicodeEncodeError:
        return False
    matched = False
    for secret in secrets:
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
        matched |= hmac.compare_digest(expected, received)
    return matched


def installation_id(payload: Mapping[str, Any]) -> int | None:
    installation = payload.get("installation")
    if isinstance(installation, Mapping):
        value = installation.get("id")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def route_event(
    event: str, payload: Mapping[str, Any], *, received_at: datetime, max_attempts: int
) -> NewJob | None:
    """The job a delivery should create, or None when CICD Doctor does not act on it."""
    action = payload.get("action")
    if event == "workflow_run" and action == "completed":
        workflow_run = _parse(_WorkflowRunEvent, event, payload)
        if workflow_run.workflow_run.conclusion == "success":
            return _outcome_job(workflow_run, max_attempts)
        return _diagnose_job(workflow_run, max_attempts)
    if event == "installation" and action in _INSTALLATION_ACTIONS:
        parsed = _parse(_InstallationPayload, event, payload)
        action_ = _INSTALLATION_ACTIONS[action]
        added = parsed.repositories if action_ is InstallationAction.CREATED else []
        return _installation_job(action_, parsed.installation, added, [], received_at, max_attempts)
    if event == "installation_repositories" and action in _REPOSITORIES_ACTIONS:
        parsed_repos = _parse(_InstallationRepositoriesPayload, event, payload)
        return _installation_job(
            _REPOSITORIES_ACTIONS[action],
            parsed_repos.installation,
            parsed_repos.repositories_added,
            [repo.id for repo in parsed_repos.repositories_removed],
            received_at,
            max_attempts,
        )
    return None


# --- GitHub payload shapes (only what we read; extra fields are ignored) -------------------


class _Owner(BaseModel):
    login: str
    type: str


class _InstallationRef(BaseModel):
    id: int


class _Repository(BaseModel):
    id: int
    full_name: str
    private: bool
    default_branch: str
    owner: _Owner


class _HeadRepository(BaseModel):
    id: int
    full_name: str


class _BaseRepo(BaseModel):
    id: int


class _PullRequestBase(BaseModel):
    repo: _BaseRepo


class _PullRequest(BaseModel):
    number: int
    base: _PullRequestBase


class _WorkflowRun(BaseModel):
    id: int
    name: str
    run_attempt: int
    workflow_id: int
    head_sha: str
    head_branch: str | None
    event: str
    conclusion: str | None
    updated_at: datetime
    head_repository: _HeadRepository | None
    pull_requests: list[_PullRequest] = []


class _WorkflowRunEvent(BaseModel):
    workflow_run: _WorkflowRun
    repository: _Repository
    installation: _InstallationRef


class _Account(BaseModel):
    login: str
    type: str


class _Installation(BaseModel):
    id: int
    account: _Account


class _ListedRepository(BaseModel):
    id: int
    full_name: str
    private: bool


class _InstallationPayload(BaseModel):
    installation: _Installation
    repositories: list[_ListedRepository] = []


class _InstallationRepositoriesPayload(BaseModel):
    installation: _Installation
    repositories_added: list[_ListedRepository] = []
    repositories_removed: list[_ListedRepository] = []


def _parse[M: BaseModel](model: type[M], event: str, payload: Mapping[str, Any]) -> M:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # Field paths only: pydantic's messages would echo (untrusted) input values.
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise InvalidPayloadError(
            f"{event} payload: invalid or missing {', '.join(fields)}"
        ) from exc


def _diagnose_job(parsed: _WorkflowRunEvent, max_attempts: int) -> NewJob | None:
    run, repo = parsed.workflow_run, parsed.repository
    if run.conclusion not in DIAGNOSED_CONCLUSIONS:
        return None
    conclusion: Literal["failure", "timed_out", "cancelled"] = (
        "failure"
        if run.conclusion == "failure"
        else "cancelled"
        if run.conclusion == "cancelled"
        else "timed_out"
    )
    run_ref = _run_ref(parsed)
    payload = DiagnoseRunPayload(
        run=run_ref,
        conclusion=conclusion,
        completed_at=run.updated_at,
        default_branch=repo.default_branch,
        private=repo.private,
        account_login=repo.owner.login,
        account_type=repo.owner.type,
    )
    return NewJob(
        kind=JobKind.DIAGNOSE_RUN,
        payload=payload.model_dump(mode="json"),
        dedupe_key=diagnose_dedupe_key(run.id, run.run_attempt),
        max_attempts=max_attempts,
    )


def _run_ref(parsed: _WorkflowRunEvent) -> RunRef:
    run, repo = parsed.workflow_run, parsed.repository
    head = run.head_repository
    # A deleted fork leaves no head repository: treat it as a fork (no automatic LLM, D-17).
    is_fork = head is None or head.id != repo.id
    return RunRef(
        installation_id=parsed.installation.id,
        repo_id=repo.id,
        repo_full_name=repo.full_name,
        run_id=run.id,
        run_attempt=run.run_attempt,
        workflow_id=run.workflow_id,
        workflow_name=run.name,
        head_sha=run.head_sha,
        head_branch=run.head_branch,
        head_repo_full_name=head.full_name if head is not None else "",
        event=run.event,
        is_fork=is_fork,
        # Fork PRs are not listed here; ``PullRequestSource`` looks them up by head repo and
        # branch (SPEC §7.1, D-72).
        pr_number=_own_pull_request(run, repo.id),
    )


def _outcome_job(parsed: _WorkflowRunEvent, max_attempts: int) -> NewJob:
    run, repo = parsed.workflow_run, parsed.repository
    payload = RecordOutcomePayload(
        run=_run_ref(parsed),
        completed_at=run.updated_at,
        default_branch=repo.default_branch,
        private=repo.private,
        account_login=repo.owner.login,
        account_type=repo.owner.type,
    )
    return NewJob(
        kind=JobKind.RECORD_OUTCOME,
        payload=payload.model_dump(mode="json"),
        dedupe_key=outcome_dedupe_key(run.id, run.run_attempt),
        max_attempts=max_attempts,
    )


def _own_pull_request(run: _WorkflowRun, repo_id: int) -> int | None:
    """GitHub may list pull requests of another repository with the same branch name: only a PR
    whose base is this repository counts (D-72)."""
    own = [pr.number for pr in run.pull_requests if pr.base.repo.id == repo_id]
    return own[0] if own else None


def _installation_job(
    action: InstallationAction,
    installation: _Installation,
    added: list[_ListedRepository],
    removed_ids: list[int],
    received_at: datetime,
    max_attempts: int,
) -> NewJob:
    event = InstallationEvent(
        action=action,
        installation=Installation(
            id=installation.id,
            account_login=installation.account.login,
            account_type=installation.account.type,
        ),
        repositories=[
            Repository(
                id=repo.id,
                installation_id=installation.id,
                full_name=repo.full_name,
                private=repo.private,
            )
            for repo in added
        ],
        removed_repository_ids=removed_ids,
        occurred_at=received_at,
    )
    # No dedupe key: the delivery id already makes the job unique (D-06).
    return NewJob(
        kind=JobKind.SYNC_INSTALLATION,
        payload=event.model_dump(mode="json"),
        max_attempts=max_attempts,
    )
