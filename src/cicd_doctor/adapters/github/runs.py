"""The workflow runs of a head commit (D-96): ``GET /actions/runs?head_sha=<sha>``.

``explain`` asks for the runs of the *current* head of a pull request. The listing is one page of
100, newest first, read with the permissions of ``explain``. Each item becomes a ``RunSummary``
whose ``RunRef`` is built like the one of the ``workflow_run`` webhook (the same ``is_fork`` rule:
a deleted head repository counts as a fork, and the same pull request rule), so a child job is the
same as the automatic one.

``pull_requests`` of a fork run is empty on the real API (spike of P3, B1) and a fork run that
waits for approval ends ``action_required``: neither is used to decide which runs to explain, that
is ``core/usecases/explain_command.py``'s job.
"""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from cicd_doctor.adapters.github.auth import EXPLAIN_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.core.models import RepoRef, RunRef, RunSummary

logger = logging.getLogger(__name__)

RUNS = "/repos/{owner}/{repo}/actions/runs"
PAGE_SIZE = 100


class _Repo(BaseModel):
    id: int
    full_name: str


class _BaseRepo(BaseModel):
    id: int


class _PullBase(BaseModel):
    repo: _BaseRepo


class _Pull(BaseModel):
    number: int
    base: _PullBase


class _Item(BaseModel):
    id: int
    name: str | None = None
    run_attempt: int
    workflow_id: int
    head_sha: str
    head_branch: str | None = None
    event: str
    status: str
    conclusion: str | None = None
    updated_at: datetime
    head_repository: _Repo | None = None
    pull_requests: list[_Pull] = []


class GitHubRunSource:
    def __init__(self, http: GitHubHttp, auth: GitHubAppAuth) -> None:
        self._http = http
        self._auth = auth

    async def runs_for_head(self, repo: RepoRef, head_sha: str) -> list[RunSummary]:
        owner, _, name = repo.full_name.partition("/")
        response = await self._http.request(
            "GET",
            RUNS,
            path={"owner": owner, "repo": name},
            query={"head_sha": head_sha, "per_page": PAGE_SIZE},
            credential=self._auth.installation_credential(
                repo.installation_id, repo.repo_id, EXPLAIN_PERMISSIONS
            ),
        )
        body = response.json()
        items = body.get("workflow_runs") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return []
        summaries: list[RunSummary] = []
        for raw in items:
            summary = _summary(raw, repo)
            if summary is not None:
                summaries.append(summary)
        return summaries


def _summary(raw: Any, repo: RepoRef) -> RunSummary | None:
    try:
        item = _Item.model_validate(raw)
    except ValidationError:
        logger.warning(
            "repo_id=%d: a run of the head listing was skipped (unexpected shape)", repo.repo_id
        )
        return None
    head = item.head_repository
    own = [pull.number for pull in item.pull_requests if pull.base.repo.id == repo.repo_id]
    run = RunRef(
        installation_id=repo.installation_id,
        repo_id=repo.repo_id,
        repo_full_name=repo.full_name,
        run_id=item.id,
        run_attempt=item.run_attempt,
        workflow_id=item.workflow_id,
        workflow_name=item.name or "",
        head_sha=item.head_sha,
        head_branch=item.head_branch,
        head_repo_full_name=head.full_name if head is not None else "",
        event=item.event,
        # A deleted fork leaves no head repository: a fork, like the webhook says (D-17).
        is_fork=head is None or head.id != repo.repo_id,
        pr_number=own[0] if own else None,
    )
    return RunSummary(
        run=run,
        status=item.status,
        conclusion=item.conclusion,
        updated_at=item.updated_at,
        head_repo_id=head.id if head is not None else None,
    )
