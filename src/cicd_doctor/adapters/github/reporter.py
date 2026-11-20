"""The ``Reporter`` over the GitHub REST API: the check run and the sticky comment (D-70, D-76).

Every call uses an installation token scoped to the repository of the run and to the diagnosis
permissions (D-68). The caller holds the publication lock and keeps the ids it gets back; an id it
hands in is a hint: when GitHub no longer knows it (404) the object is searched for, and only then
created.

- The check run is identified by its ``external_id`` (``cicd-doctor:run:{run_id}``), never by its
  name: the name is the workflow's (untrusted, editable) and several check runs may share it.
- The comment is identified by the marker on its first line, and only among the comments written
  by this app (``<slug>[bot]``): a person who copies the marker into their own comment is ignored,
  and a comment the bot did not write is never edited.
- Nothing here puts a response body in an error: ``GitHubHttp`` already keeps them out.
"""

from datetime import UTC
from typing import Any

import httpx2

from cicd_doctor.adapters.github.auth import (
    DIAGNOSIS_PERMISSIONS,
    EXPLAIN_PERMISSIONS,
    GitHubAppAuth,
)
from cicd_doctor.adapters.github.http import Credential, GitHubHttp, GitHubPermanentError
from cicd_doctor.core.errors import CommentGone, PermanentError, TransientError
from cicd_doctor.core.models import CheckOutput, CommentRef, CommentTarget, RunRef, Suggestion
from cicd_doctor.core.ports import Clock
from cicd_doctor.core.render import check_name

CHECK_RUNS = "/repos/{owner}/{repo}/check-runs"
CHECK_RUN = "/repos/{owner}/{repo}/check-runs/{check_run_id}"
COMMIT_CHECK_RUNS = "/repos/{owner}/{repo}/commits/{ref}/check-runs"
ISSUE_COMMENTS = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
ISSUE_COMMENT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"
COMMENT_REACTIONS = "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"

COMMENT_PAGES = 10  # of 100 comments: more than 1,000 comments on one PR is an accepted residual
CHECK_RUN_PAGES = 10
MAX_TITLE_CHARS = 255
MAX_SUMMARY_CHARS = 65_535  # the Checks API limits (``core.render`` stays well below)


def external_id_for(run_id: int) -> str:
    return f"cicd-doctor:run:{run_id}"


def comment_marker(pr_number: int, workflow_id: int) -> str:
    """The first line of the sticky comment of one (PR, workflow); ``core.render`` writes it."""
    return f"<!-- cicd-doctor:v1 pr={int(pr_number)} workflow={int(workflow_id)} -->"


class GitHubReporter:
    def __init__(self, http: GitHubHttp, auth: GitHubAppAuth, clock: Clock) -> None:
        self._http = http
        self._auth = auth
        self._clock = clock

    # --- check run -------------------------------------------------------------------------

    async def start_check(self, run: RunRef, known_id: int | None) -> int:
        credential = self._credential(run)
        progress = _progress(run)
        if known_id is not None and await self._reopen(run, credential, known_id, progress):
            return known_id
        existing = await self._find_check(run, credential)
        if existing is not None and await self._reopen(run, credential, existing, progress):
            return existing
        response = await self._http.request(
            "POST",
            CHECK_RUNS,
            path=_repo_path(run),
            json={
                "name": check_name(run.workflow_name),
                "head_sha": run.head_sha,
                "status": "in_progress",
                "external_id": external_id_for(run.run_id),
                "details_url": _run_url(run),
                "output": progress,
            },
            credential=credential,
        )
        return _id_of(response, f"POST {CHECK_RUNS}")

    async def complete_check(self, run: RunRef, check_run_id: int, output: CheckOutput) -> None:
        now = self._clock.now().astimezone(UTC)
        await self._http.request(
            "PATCH",
            CHECK_RUN,
            path={**_repo_path(run), "check_run_id": check_run_id},
            json={
                "status": "completed",
                "conclusion": "neutral",  # never blocks a merge (D-18)
                "completed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "output": {
                    "title": output.title[:MAX_TITLE_CHARS],
                    "summary": output.summary[:MAX_SUMMARY_CHARS],
                },
            },
            credential=self._credential(run),
        )

    async def _reopen(
        self, run: RunRef, credential: Credential, check_run_id: int, progress: dict[str, str]
    ) -> bool:
        """Put an existing check run in progress, with the current name of the workflow. ``False``
        when GitHub does not know it (404).

        GitHub does not reopen a completed check run (U10, measured on 2026-11-16): the ``PATCH``
        answers 200, the check stays ``completed`` with its conclusion, and ``name`` and
        ``output`` are applied. That is normal here: the answer is not inspected and nothing is
        retried. The 422 branch below is defensive only: GitHub has never been seen refusing it."""
        path: dict[str, str | int] = {**_repo_path(run), "check_run_id": check_run_id}
        body: dict[str, Any] = {
            "name": check_name(run.workflow_name),
            "status": "in_progress",
            "output": progress,
        }
        try:
            await self._http.request(
                "PATCH", CHECK_RUN, path=path, json=body, credential=credential
            )
        except GitHubPermanentError as exc:
            if exc.status_code == 404:
                return False
            if exc.status_code != 422:
                raise
            # Never observed (see above): kept in case GitHub starts refusing; only the output.
            try:
                await self._http.request(
                    "PATCH",
                    CHECK_RUN,
                    path=path,
                    json={"output": progress},
                    credential=credential,
                )
            except GitHubPermanentError as again:
                if again.status_code == 404:
                    return False
                raise
        return True

    async def _find_check(self, run: RunRef, credential: Credential) -> int | None:
        """Our check run of this run among those of the commit: by ``external_id``, as ours."""
        identity = await self._auth.app_identity()
        wanted = external_id_for(run.run_id)
        listed = await self._http.paginate(
            COMMIT_CHECK_RUNS,
            path={**_repo_path(run), "ref": run.head_sha},
            query={"app_id": identity.id, "filter": "all"},
            credential=credential,
            items_key="check_runs",
            max_pages=CHECK_RUN_PAGES,
        )
        ours = [
            item["id"]
            for item in listed.items
            if isinstance(item, dict)
            and item.get("external_id") == wanted
            and isinstance(item.get("app"), dict)
            and item["app"].get("id") == identity.id
            and isinstance(item.get("id"), int)
        ]
        return min(ours) if ours else None

    # --- comment ---------------------------------------------------------------------------

    async def upsert_comment(self, target: CommentTarget, body: str, known_id: int | None) -> int:
        marker = comment_marker(target.pr_number, target.run.workflow_id)
        if _first_line(body) != marker:
            # Without its marker a comment could never be found again: refuse to write it.
            raise PermanentError("the comment body does not start with the marker of its target")
        run = target.run
        credential = self._credential(run)
        if known_id is not None and await self._edit(run, credential, known_id, body):
            return known_id
        existing = await self._find_comment(target, credential, marker)
        if existing is not None and await self._edit(run, credential, existing, body):
            return existing
        response = await self._http.request(
            "POST",
            ISSUE_COMMENTS,
            path={**_repo_path(run), "issue_number": target.pr_number},
            json={"body": body},
            credential=credential,
        )
        return _id_of(response, f"POST {ISSUE_COMMENTS}")

    async def _edit(self, run: RunRef, credential: Credential, comment_id: int, body: str) -> bool:
        """``False`` when the comment does not exist any more (404)."""
        try:
            await self._http.request(
                "PATCH",
                ISSUE_COMMENT,
                path={**_repo_path(run), "comment_id": comment_id},
                json={"body": body},
                credential=credential,
            )
        except GitHubPermanentError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    async def _find_comment(
        self, target: CommentTarget, credential: Credential, marker: str
    ) -> int | None:
        """The comment of this (PR, workflow) written by this app, the oldest if there are several.

        Only ``<slug>[bot]`` counts: anyone can type the marker into a comment of their own."""
        identity = await self._auth.app_identity()
        bot = f"{identity.slug}[bot]"
        listed = await self._http.paginate(
            ISSUE_COMMENTS,
            path={**_repo_path(target.run), "issue_number": target.pr_number},
            credential=credential,
            max_pages=COMMENT_PAGES,
        )
        ours = [
            item["id"]
            for item in listed.items
            if isinstance(item, dict)
            and isinstance(item.get("user"), dict)
            and item["user"].get("login") == bot
            and isinstance(item.get("body"), str)
            and _first_line(item["body"]) == marker
            and isinstance(item.get("id"), int)
        ]
        return min(ours) if ours else None

    # --- P3 ---------------------------------------------------------------------------------

    async def post_suggestion(self, target: CommentTarget, suggestion: Suggestion) -> None:
        raise NotImplementedError("P3")

    async def react(self, comment: CommentRef, reaction: str) -> None:
        """``POST .../issues/comments/{id}/reactions``: 201 when created, 200 when the app had
        already left this reaction (both are success). A 404 is a deleted comment."""
        owner, _, repo = comment.repo_full_name.partition("/")
        credential = self._auth.installation_credential(
            comment.installation_id, comment.repo_id, EXPLAIN_PERMISSIONS
        )
        try:
            await self._http.request(
                "POST",
                COMMENT_REACTIONS,
                path={"owner": owner, "repo": repo, "comment_id": comment.comment_id},
                json={"content": reaction},
                credential=credential,
            )
        except GitHubPermanentError as exc:
            if exc.status_code == 404:
                raise CommentGone("the comment no longer exists") from exc
            if exc.status_code == 403:
                raise GitHubPermanentError(
                    f"reaction refused: the installation has not accepted Issues: write ({exc})",
                    status_code=403,
                ) from exc
            raise

    # --- helpers ---------------------------------------------------------------------------

    def _credential(self, run: RunRef) -> Credential:
        return self._auth.installation_credential(
            run.installation_id, run.repo_id, DIAGNOSIS_PERMISSIONS
        )


def _repo_path(run: RunRef) -> dict[str, str]:
    owner, _, repo = run.repo_full_name.partition("/")
    return {"owner": owner, "repo": repo}


def _run_url(run: RunRef) -> str:
    return f"https://github.com/{run.repo_full_name}/actions/runs/{int(run.run_id)}"


def _progress(run: RunRef) -> dict[str, str]:
    return {
        "title": "Analyzing failure…",
        "summary": f"Analyzing failure… (attempt {int(run.run_attempt)})",
    }


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0].rstrip("\r")


def _id_of(response: httpx2.Response, label: str) -> int:
    try:
        value = response.json()["id"]
    except (ValueError, KeyError, TypeError):
        value = None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TransientError(f"{label} returned an unexpected body")
    return value
