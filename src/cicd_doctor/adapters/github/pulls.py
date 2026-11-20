"""The pull request of a run (D-72).

The webhook already took the number from ``workflow_run.pull_requests`` when GitHub listed one
that belongs to this repository. That list is empty for fork pull requests and for
``workflow_dispatch``, so for a fork run or a ``pull_request*`` event the open pull requests of
the branch are searched, and only one that matches the run exactly is accepted.
"""

from pydantic import BaseModel, ValidationError

from cicd_doctor.adapters.github.auth import (
    DIAGNOSIS_PERMISSIONS,
    EXPLAIN_PERMISSIONS,
    GitHubAppAuth,
)
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubPermanentError
from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import DiffFile, PullDiff, PullRequestInfo, RepoRef, RunRef

PULLS = "/repos/{owner}/{repo}/pulls"
PULL = "/repos/{owner}/{repo}/pulls/{number}"
PULL_FILES = "/repos/{owner}/{repo}/pulls/{number}/files"
MAX_PAGES = 3
FILES_PAGES = 30  # of 100 files: GitHub lists at most 3,000 files of a pull request


class GitHubPullRequests:
    def __init__(self, http: GitHubHttp, auth: GitHubAppAuth) -> None:
        self._http = http
        self._auth = auth

    async def find(self, run: RunRef) -> int | None:
        if run.pr_number is not None:
            return run.pr_number
        if not (run.is_fork or run.event.startswith("pull_request")):
            return None
        head_owner = run.head_repo_full_name.partition("/")[0]
        if not head_owner or not run.head_branch:
            return None  # a deleted fork: nothing to search for
        owner, _, repo = run.repo_full_name.partition("/")
        listed = await self._http.paginate(
            PULLS,
            path={"owner": owner, "repo": repo},
            query={"head": f"{head_owner}:{run.head_branch}", "state": "open"},
            credential=self._auth.installation_credential(
                run.installation_id, run.repo_id, DIAGNOSIS_PERMISSIONS
            ),
            max_pages=MAX_PAGES,
        )
        numbers = [
            item["number"]
            for item in listed.items
            if isinstance(item, dict)
            and isinstance(item.get("number"), int)
            and _matches(item, run)
        ]
        return min(numbers) if numbers else None

    async def get(
        self, repo: RepoRef, number: int, *, for_explain: bool = True
    ) -> PullRequestInfo | None:
        """``GET /pulls/{n}`` with the permissions of ``explain`` (D-96) or of the diagnosis
        (``for_explain=False``, D-97). A 404 is ``None``."""
        permissions = EXPLAIN_PERMISSIONS if for_explain else DIAGNOSIS_PERMISSIONS
        owner, _, name = repo.full_name.partition("/")
        try:
            response = await self._http.request(
                "GET",
                PULL,
                path={"owner": owner, "repo": name, "number": number},
                credential=self._auth.installation_credential(
                    repo.installation_id, repo.repo_id, permissions
                ),
            )
        except GitHubPermanentError as exc:
            if exc.status_code == 404:
                return None
            raise
        try:
            raw = _Pull.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise PermanentError("GET /pulls/{number}: the answer has an unexpected shape") from exc
        head_repo = raw.head.repo
        return PullRequestInfo(
            number=raw.number,
            state="open" if raw.state == "open" else "closed",
            draft=raw.draft,
            merged=raw.merged,
            head_sha=raw.head.sha,
            head_branch=raw.head.ref,
            head_repo_id=head_repo.id if head_repo is not None else None,
            head_repo_full_name=head_repo.full_name if head_repo is not None else "",
            base_repo_id=raw.base.repo.id,
        )

    async def diff(self, repo: RepoRef, number: int) -> PullDiff:
        """``GET /pulls/{n}/files``, every page up to the cap, with the permissions of the
        diagnosis (D-97). An item of an unexpected shape is skipped; a 404 is an empty diff."""
        owner, _, name = repo.full_name.partition("/")
        try:
            listed = await self._http.paginate(
                PULL_FILES,
                path={"owner": owner, "repo": name, "number": number},
                credential=self._auth.installation_credential(
                    repo.installation_id, repo.repo_id, DIAGNOSIS_PERMISSIONS
                ),
                max_pages=FILES_PAGES,
            )
        except GitHubPermanentError as exc:
            if exc.status_code == 404:
                return PullDiff()
            raise
        files: list[DiffFile] = []
        for item in listed.items:
            try:
                raw = _File.model_validate(item)
            except ValidationError:
                continue
            files.append(
                DiffFile(
                    filename=raw.filename,
                    previous_filename=raw.previous_filename,
                    status=raw.status,
                    patch=raw.patch,
                )
            )
        return PullDiff(files=files, truncated=listed.truncated)


class _File(BaseModel):
    filename: str
    previous_filename: str | None = None
    status: str
    patch: str | None = None  # absent for binary, renamed-only, removed and very large files


class _HeadRepo(BaseModel):
    id: int
    full_name: str


class _Head(BaseModel):
    sha: str
    ref: str
    repo: _HeadRepo | None = None


class _BaseRepo(BaseModel):
    id: int


class _Base(BaseModel):
    repo: _BaseRepo


class _Pull(BaseModel):
    number: int
    state: str
    draft: bool = False
    merged: bool = False
    head: _Head
    base: _Base


def _matches(pull: dict[str, object], run: RunRef) -> bool:
    head = pull.get("head")
    if not isinstance(head, dict) or head.get("sha") != run.head_sha:
        return False
    repo = head.get("repo")
    return isinstance(repo, dict) and repo.get("full_name") == run.head_repo_full_name
