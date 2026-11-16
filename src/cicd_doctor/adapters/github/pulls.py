"""The pull request of a run (D-72).

The webhook already took the number from ``workflow_run.pull_requests`` when GitHub listed one
that belongs to this repository. That list is empty for fork pull requests and for
``workflow_dispatch``, so for a fork run or a ``pull_request*`` event the open pull requests of
the branch are searched, and only one that matches the run exactly is accepted.
"""

from cicd_doctor.adapters.github.auth import DIAGNOSIS_PERMISSIONS, GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.core.models import RunRef

PULLS = "/repos/{owner}/{repo}/pulls"
MAX_PAGES = 3


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


def _matches(pull: dict[str, object], run: RunRef) -> bool:
    head = pull.get("head")
    if not isinstance(head, dict) or head.get("sha") != run.head_sha:
        return False
    repo = head.get("repo")
    return isinstance(repo, dict) and repo.get("full_name") == run.head_repo_full_name
