"""A stateful fake of the GitHub API over ``httpx2.MockTransport`` (D-67).

The real adapters run against it, so a test sees the exact requests they send. ``FakeGitHub``:

- records every request with its headers (``FakeGitHub.requests``). A request's ``repr`` never
  shows the ``Authorization`` value, and the ``has_bearer`` and ``has_header`` helpers let a test
  assert on credentials without putting them in an assertion message;
- keeps state: installations (suspended or not, with their repositories and permissions) and the
  installation tokens it issued (they expire with the injected clock and can be revoked);
- has a route registry: ``add_route(method, template, handler, auth=...)``. The next PRs add their
  routes (logs, check runs, comments, pulls) with it and do not touch the rest. The router checks
  the credential the route asks for before the handler runs, as GitHub does;
- fails on demand: ``fail(...)`` answers a status, headers, a delay or a raised transport error
  instead of the route, a given number of times. ``delay(...)`` slows a route down so concurrent
  callers overlap;
- puts ``CANARY`` in the body of every error it answers. A test that sees the canary in an
  exception message or a log line has found a leak of a response body.

Everything here is made up: the keys are generated per test session and no token looks like a
real one (D-59).
"""

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from cicd_doctor.adapters.clock import FakeClock

CANARY = "canary-7c1e9a-must-never-leak"
APP_ID = 424242
APP_SLUG = "cicd-doctor-test"
INSTALLATION_ID = 1001
REPO_ID = 2002
API_HOST = "api.github.com"

_LEVELS = {"read": 1, "write": 2, "admin": 3}
DEFAULT_INSTALLATION_PERMISSIONS = {
    "actions": "read",
    "checks": "write",
    "contents": "read",
    "metadata": "read",
    "pull_requests": "write",
}

Auth = Literal["app", "installation", "none"]


@dataclass(frozen=True, repr=False)
class RecordedRequest:
    method: str
    host: str
    path: str  # as sent: still percent-encoded
    raw_query: str
    headers: Mapping[str, str]  # lower-case names
    json: Any
    template: str | None  # the route that matched, ``None`` if none did
    params: Mapping[str, str]  # the placeholders of the template, decoded

    @property
    def query(self) -> dict[str, str]:
        return dict(parse_qsl(self.raw_query))

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def has_authorization(self) -> bool:
        return "authorization" in self.headers

    def has_header(self, name: str) -> bool:
        return name.lower() in self.headers

    def has_bearer(self, token: str) -> bool:
        return self.headers.get("authorization") == f"Bearer {token}"

    def __repr__(self) -> str:  # never the headers: they carry the credential
        query = f"?{self.raw_query}" if self.raw_query else ""
        return f"<{self.method} {self.host}{self.path}{query}>"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    installation_id: int
    repo_ids: tuple[int, ...]
    permissions: Mapping[str, str]
    expires_at: datetime


@dataclass
class InstallationState:
    repo_ids: set[int]
    permissions: dict[str, str]
    suspended: bool = False


Handler = Callable[[RecordedRequest], httpx2.Response | Awaitable[httpx2.Response]]


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    pattern: re.Pattern[str]
    handler: Handler
    auth: Auth


@dataclass
class Failure:
    method: str
    template: str  # a route template, or "*" for any request
    status: int
    headers: dict[str, str]
    message: str
    delay: float
    error: Exception | None
    times: int | None  # how many requests it still fails; ``None``: always


def json_response(
    status: int, body: Any, headers: Mapping[str, str] | None = None
) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=dict(headers or {}))


def error_response(
    status: int, message: str, headers: Mapping[str, str] | None = None
) -> httpx2.Response:
    """GitHub's error shape. The canary rides in the message, where a leak would show."""
    body = {
        "message": f"{message} {CANARY}",
        "documentation_url": "https://docs.github.com/rest",
        "status": str(status),
    }
    return json_response(status, body, headers)


def _compile(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{([a-z_]+)\}", template)
    regex = "".join(
        f"(?P<{part}>[^/]+)" if index % 2 else re.escape(part) for index, part in enumerate(parts)
    )
    return re.compile(regex)


@dataclass
class FakeGitHub:
    clock: FakeClock
    app_public_key: RSAPublicKey
    app_id: int = APP_ID
    app_slug: str = APP_SLUG
    token_ttl: timedelta = timedelta(hours=1)
    requests: list[RecordedRequest] = field(default_factory=list)
    installations: dict[int, InstallationState] = field(default_factory=dict)
    issued: list[IssuedToken] = field(default_factory=list)
    _routes: list[Route] = field(default_factory=list)
    _failures: list[Failure] = field(default_factory=list)
    _delays: dict[tuple[str, str], float] = field(default_factory=dict)
    _revoked: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.add_route("GET", "/app", self._get_app, auth="app")
        self.add_route(
            "POST",
            "/app/installations/{installation_id}/access_tokens",
            self._create_token,
            auth="app",
        )

    # --- wiring --------------------------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    def add_route(
        self, method: str, template: str, handler: Handler, *, auth: Auth = "installation"
    ) -> None:
        """Answer ``method`` on ``template`` (``/repos/{owner}/{repo}/...``) with ``handler``.

        A route added later wins over an earlier one for the same request, so a test can replace
        a built-in route (for example to answer a malformed body).
        """
        self._routes.insert(0, Route(method, template, _compile(template), handler, auth))

    # --- state ---------------------------------------------------------------------------

    def install(
        self,
        installation_id: int = INSTALLATION_ID,
        repo_ids: set[int] | None = None,
        permissions: Mapping[str, str] | None = None,
    ) -> InstallationState:
        state = InstallationState(
            repo_ids={REPO_ID} if repo_ids is None else set(repo_ids),
            permissions=dict(
                DEFAULT_INSTALLATION_PERMISSIONS if permissions is None else permissions
            ),
        )
        self.installations[installation_id] = state
        return state

    def revoke(self, token: str) -> None:
        """GitHub no longer accepts ``token`` (it answers 401)."""
        self._revoked.add(token)

    def revoke_all(self) -> None:
        self._revoked.update(issued.token for issued in self.issued)

    # --- programmable misbehaviour -------------------------------------------------------

    def fail(
        self,
        method: str,
        template: str,
        status: int = 500,
        *,
        headers: Mapping[str, str] | None = None,
        message: str = "Server Error",
        delay: float = 0.0,
        error: Exception | None = None,
        times: int | None = 1,
    ) -> None:
        """Answer ``status`` (or raise ``error``, a transport error) instead of the route."""
        self._failures.append(
            Failure(method, template, status, dict(headers or {}), message, delay, error, times)
        )

    def delay(self, method: str, template: str, seconds: float) -> None:
        """Make a route take ``seconds`` to answer, so concurrent callers overlap."""
        self._delays[(method, template)] = seconds

    # --- what happened -------------------------------------------------------------------

    def requests_to(self, method: str, template: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == method and r.template == template]

    # --- the transport -------------------------------------------------------------------

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        route, params = self._match(request)
        recorded = RecordedRequest(
            method=request.method,
            host=request.url.host,
            path=request.url.raw_path.decode().partition("?")[0],
            raw_query=request.url.query.decode(),
            headers={name.lower(): value for name, value in request.headers.items()},
            json=_json_body(request),
            template=route.template if route else None,
            params=params,
        )
        self.requests.append(recorded)

        failure = self._take_failure(recorded)
        slow = self._delays.get((recorded.method, recorded.template or ""), 0.0)
        pause = failure.delay if failure is not None else slow
        if pause:
            await asyncio.sleep(pause)
        if failure is not None:
            if failure.error is not None:
                raise failure.error
            return error_response(failure.status, failure.message, failure.headers)
        if route is None:
            return error_response(404, "Not Found")

        denied = self._authenticate(route, recorded)
        if denied is not None:
            return denied
        outcome = route.handler(recorded)
        return await outcome if inspect.isawaitable(outcome) else outcome

    def _match(self, request: httpx2.Request) -> tuple[Route | None, dict[str, str]]:
        path = request.url.raw_path.decode().partition("?")[0]
        for route in self._routes:
            match = route.pattern.fullmatch(path)
            if route.method == request.method and match:
                return route, {k: unquote(v) for k, v in match.groupdict().items()}
        return None, {}

    def _take_failure(self, request: RecordedRequest) -> Failure | None:
        for failure in self._failures:
            if failure.method != request.method or failure.template not in ("*", request.template):
                continue
            if failure.times is not None:
                failure.times -= 1
                if failure.times <= 0:
                    self._failures.remove(failure)
            return failure
        return None

    # --- credentials ---------------------------------------------------------------------

    def _authenticate(self, route: Route, request: RecordedRequest) -> httpx2.Response | None:
        if route.auth == "none":
            return None
        header = request.header("authorization") or ""
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        if route.auth == "app":
            if not self._valid_jwt(token):
                return error_response(401, "A JSON web token could not be decoded")
        elif not self._valid_installation_token(token):
            return error_response(401, "Bad credentials")
        return None

    def _valid_jwt(self, token: str) -> bool:
        """GitHub's rules: RS256, the app id as ``iss``, not expired, at most 10 minutes ahead."""
        try:
            claims = jwt.decode(
                token,
                self.app_public_key,
                algorithms=["RS256"],
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["iss", "iat", "exp"],
                },
            )
        except jwt.PyJWTError:
            return False
        now = self.clock.now().timestamp()
        return bool(
            claims["iss"] == str(self.app_id)
            and claims["iat"] <= now
            and now < claims["exp"] <= now + 600
        )

    def _valid_installation_token(self, token: str) -> bool:
        now = self.clock.now()
        return any(
            issued.token == token and issued.token not in self._revoked and now < issued.expires_at
            for issued in self.issued
        )

    # --- built-in routes -----------------------------------------------------------------

    def _get_app(self, request: RecordedRequest) -> httpx2.Response:
        return json_response(
            200,
            {
                "id": self.app_id,
                "slug": self.app_slug,
                "name": "CICD Doctor (test)",
                "permissions": DEFAULT_INSTALLATION_PERMISSIONS,
                "events": ["workflow_run"],
            },
        )

    def _create_token(self, request: RecordedRequest) -> httpx2.Response:
        installation = self.installations.get(int(request.params["installation_id"]))
        if installation is None:
            return error_response(404, "Not Found")
        if installation.suspended:
            return error_response(403, "This installation has been suspended")
        body = request.json if isinstance(request.json, dict) else {}
        repo_ids = tuple(body.get("repository_ids", []))
        if not set(repo_ids) <= installation.repo_ids:
            return error_response(
                422, "There is at least one repository that does not exist or is not accessible"
            )
        requested: dict[str, str] = body.get("permissions", {})
        for name, level in requested.items():
            if _LEVELS.get(installation.permissions.get(name, ""), 0) < _LEVELS.get(level, 99):
                return error_response(422, "The permissions requested are not granted")
        expires_at = self.clock.now() + self.token_ttl
        token = f"fake-installation-token-{len(self.issued) + 1:03d}"
        self.issued.append(
            IssuedToken(
                token, int(request.params["installation_id"]), repo_ids, requested, expires_at
            )
        )
        return json_response(
            201,
            {
                "token": token,
                "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": {**requested, "metadata": "read"},
                "repository_selection": "selected",
                "repositories": [
                    {"id": repo_id, "name": f"repo-{repo_id}"} for repo_id in repo_ids
                ],
            },
        )


def _json_body(request: httpx2.Request) -> Any:
    return json.loads(request.content) if request.content else None


# ==== P1c (c): check runs, issue comments and pulls ======================================
# Self-contained: ``FakeRepoWrites(github)`` registers its routes with ``add_route`` and keeps the
# state they read and write. Shapes follow what the spike measured on the real API (check run
# objects, ``Link`` headers); the bot comment shape is the documented one (``user.login`` is
# ``<slug>[bot]``, ``user.type`` is ``Bot``), never observed yet.

CHECK_STATUSES = frozenset({"queued", "in_progress", "completed"})
MAX_CHECK_SUMMARY = 65_535  # the Checks API limit
MAX_COMMENT_BODY = 65_536


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FakeRepoWrites:
    """The write side of the repository API: check runs, issue comments, and the pulls listing.

    ``allow_reopen=False`` makes ``PATCH`` refuse (422) to take a completed check run back to
    ``in_progress``: nobody has verified yet what GitHub does (U10), so tests run both.
    """

    github: FakeGitHub
    allow_reopen: bool = True
    page_cap: int = 100  # lower it to make a listing paginate with a few items
    check_runs: dict[int, dict[str, Any]] = field(default_factory=dict)
    comments: dict[int, dict[str, Any]] = field(default_factory=dict)
    pulls: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 9000

    def __post_init__(self) -> None:
        base = "/repos/{owner}/{repo}"
        add = self.github.add_route
        add("POST", f"{base}/check-runs", self._create_check_run)
        add("PATCH", f"{base}/check-runs/{{check_run_id}}", self._update_check_run)
        add("GET", f"{base}/commits/{{ref}}/check-runs", self._list_check_runs)
        add("GET", f"{base}/issues/{{issue_number}}/comments", self._list_comments)
        add("POST", f"{base}/issues/{{issue_number}}/comments", self._create_comment)
        add("PATCH", f"{base}/issues/comments/{{comment_id}}", self._update_comment)
        add("GET", f"{base}/pulls", self._list_pulls)

    # --- state the tests set up ----------------------------------------------------------

    def add_check_run(
        self,
        *,
        repo: str = "acme/widgets",
        name: str = "run",
        head_sha: str,
        external_id: str | None = None,
        status: str = "completed",
        conclusion: str | None = "success",
        app_id: int | None = None,
    ) -> dict[str, Any]:
        """A check run somebody else (or an earlier run of ours) already made."""
        run = self._check_run_object(repo, name, head_sha, external_id, status, app_id)
        run["conclusion"] = conclusion if status == "completed" else None
        return run

    def add_user_comment(
        self, pr_number: int, body: str, *, login: str = "mallory", repo: str = "acme/widgets"
    ) -> dict[str, Any]:
        """A comment written by a person (or any account that is not the bot)."""
        return self._comment_object(repo, pr_number, body, login=login, user_type="User")

    def add_bot_comment(
        self, pr_number: int, body: str, *, repo: str = "acme/widgets"
    ) -> dict[str, Any]:
        return self._comment_object(
            repo, pr_number, body, login=f"{self.github.app_slug}[bot]", user_type="Bot"
        )

    def add_pull(
        self,
        number: int,
        *,
        head_repo: str,
        head_ref: str,
        head_sha: str,
        base_repo_id: int = REPO_ID,
        state: str = "open",
        repo: str = "acme/widgets",
    ) -> dict[str, Any]:
        """A pull request; ``head_repo=""`` is a fork that was deleted (``head.repo`` is null)."""
        owner = head_repo.split("/")[0] if head_repo else "ghost"
        pull = {
            "number": number,
            "state": state,
            "head": {
                "ref": head_ref,
                "sha": head_sha,
                "repo": {"full_name": head_repo} if head_repo else None,
                "label": f"{owner}:{head_ref}",
            },
            "base": {"ref": "main", "repo": {"id": base_repo_id, "full_name": repo}},
            "_repo": repo,
        }
        self.pulls.append(pull)
        return pull

    def requests_by(self, method: str, template: str) -> list[RecordedRequest]:
        return self.github.requests_to(method, template)

    # --- objects -------------------------------------------------------------------------

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _check_run_object(
        self,
        repo: str,
        name: str,
        head_sha: str,
        external_id: str | None,
        status: str,
        app_id: int | None,
    ) -> dict[str, Any]:
        check_id = self._new_id()
        run = {
            "id": check_id,
            "name": name,
            "head_sha": head_sha,
            "external_id": external_id if external_id is not None else f"uuid-{check_id}",
            "status": status,
            "conclusion": None,
            "started_at": _timestamp(self.github.clock.now()),
            "completed_at": None,
            "details_url": None,
            "output": {
                "title": None,
                "summary": None,
                "text": None,
                "annotations_count": 0,
                "annotations_url": f"https://api.github.com/repos/{repo}/check-runs/{check_id}"
                "/annotations",
            },
            "check_suite": {"id": 8000 + check_id},
            "app": {
                "id": self.github.app_id if app_id is None else app_id,
                "slug": self.github.app_slug if app_id is None else f"app-{app_id}",
            },
            "_repo": repo,
        }
        self.check_runs[check_id] = run
        return run

    def _comment_object(
        self, repo: str, pr_number: int, body: str, *, login: str, user_type: str
    ) -> dict[str, Any]:
        comment_id = self._new_id()
        now = _timestamp(self.github.clock.now())
        comment: dict[str, Any] = {
            "id": comment_id,
            "body": body,
            "user": {"login": login, "type": user_type},
            "performed_via_github_app": (
                {"id": self.github.app_id, "slug": self.github.app_slug}
                if user_type == "Bot"
                else None
            ),
            "created_at": now,
            "updated_at": now,
            "_repo": repo,
            "_pr": pr_number,
        }
        self.comments[comment_id] = comment
        return comment

    # --- permissions ---------------------------------------------------------------------

    def _lacks(
        self, request: RecordedRequest, permission: str, level: str
    ) -> httpx2.Response | None:
        """403 when the installation token was not requested with ``permission`` at ``level``."""
        header = request.header("authorization") or ""
        token = header.removeprefix("Bearer ")
        issued = next((i for i in self.github.issued if i.token == token), None)
        granted = _LEVELS.get((issued.permissions if issued else {}).get(permission, ""), 0)
        if granted < _LEVELS[level]:
            return error_response(403, f"Resource not accessible: needs {permission}={level}")
        return None

    # --- check runs ----------------------------------------------------------------------

    def _create_check_run(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "checks", "write")) is not None:
            return denied
        body = request.json if isinstance(request.json, dict) else {}
        status = body.get("status", "queued")
        if not body.get("name") or not body.get("head_sha") or status not in CHECK_STATUSES:
            return error_response(422, "Validation Failed")
        repo = f"{request.params['owner']}/{request.params['repo']}"
        problem = self._check_output_problem(body.get("output"))
        if problem is not None:
            return problem
        run = self._check_run_object(
            repo, body["name"], body["head_sha"], body.get("external_id"), status, None
        )
        run["details_url"] = body.get("details_url")
        self._apply_output(run, body.get("output"))
        return json_response(201, _public(run))

    def _update_check_run(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "checks", "write")) is not None:
            return denied
        run = self.check_runs.get(int(request.params["check_run_id"]))
        if run is None:
            return error_response(404, "Not Found")
        body = request.json if isinstance(request.json, dict) else {}
        status = body.get("status", run["status"])
        if status not in CHECK_STATUSES:
            return error_response(422, "Validation Failed")
        if "conclusion" in body and status != "completed":
            return error_response(422, "conclusion needs status completed")
        if run["status"] == "completed" and status != "completed" and not self.allow_reopen:
            return error_response(422, "A completed check run cannot be reopened")
        problem = self._check_output_problem(body.get("output"))
        if problem is not None:
            return problem
        self._apply_output(run, body.get("output"))
        for key in ("name", "details_url", "conclusion", "completed_at", "external_id"):
            if key in body:
                run[key] = body[key]
        if status != "completed":
            run["conclusion"], run["completed_at"] = None, None
        run["status"] = status
        return json_response(200, _public(run))

    @staticmethod
    def _check_output_problem(output: Any) -> httpx2.Response | None:
        if output is None:
            return None
        if not isinstance(output, dict) or not output.get("title") or "summary" not in output:
            return error_response(422, "output needs a title and a summary")
        if len(output["summary"]) > MAX_CHECK_SUMMARY:
            return error_response(422, "output.summary is too long")
        return None

    @staticmethod
    def _apply_output(run: dict[str, Any], output: Any) -> None:
        if output is not None:
            run["output"] = {
                **run["output"],
                "title": output["title"],
                "summary": output["summary"],
            }

    def _list_check_runs(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "checks", "read")) is not None:
            return denied
        repo = f"{request.params['owner']}/{request.params['repo']}"
        query = request.query
        found = [
            run
            for run in self.check_runs.values()
            if run["_repo"] == repo
            and run["head_sha"] == request.params["ref"]
            and ("app_id" not in query or str(run["app"]["id"]) == query["app_id"])
            and ("check_name" not in query or run["name"] == query["check_name"])
        ]
        page, link = _page(request, self.page_cap, [_public(run) for run in found])
        return json_response(200, {"total_count": len(found), "check_runs": page}, link)

    # --- comments ------------------------------------------------------------------------

    def _list_comments(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "pull_requests", "write")) is not None:
            return denied
        repo = f"{request.params['owner']}/{request.params['repo']}"
        number = int(request.params["issue_number"])
        found = [c for c in self.comments.values() if c["_repo"] == repo and c["_pr"] == number]
        page, link = _page(request, self.page_cap, [_public(c) for c in found])
        return json_response(200, page, link)

    def _create_comment(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "pull_requests", "write")) is not None:
            return denied
        body = request.json if isinstance(request.json, dict) else {}
        text = body.get("body")
        if not isinstance(text, str) or not text or len(text) > MAX_COMMENT_BODY:
            return error_response(422, "Validation Failed")
        repo = f"{request.params['owner']}/{request.params['repo']}"
        comment = self._comment_object(
            repo,
            int(request.params["issue_number"]),
            text,
            login=f"{self.github.app_slug}[bot]",
            user_type="Bot",
        )
        return json_response(201, _public(comment))

    def _update_comment(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "pull_requests", "write")) is not None:
            return denied
        comment = self.comments.get(int(request.params["comment_id"]))
        if comment is None:
            return error_response(404, "Not Found")
        if comment["user"]["login"] != f"{self.github.app_slug}[bot]":
            return error_response(403, "Resource not accessible by integration")
        body = request.json if isinstance(request.json, dict) else {}
        text = body.get("body")
        if not isinstance(text, str) or not text or len(text) > MAX_COMMENT_BODY:
            return error_response(422, "Validation Failed")
        comment["body"] = text
        comment["updated_at"] = _timestamp(self.github.clock.now())
        return json_response(200, _public(comment))

    # --- pulls ---------------------------------------------------------------------------

    def _list_pulls(self, request: RecordedRequest) -> httpx2.Response:
        if (denied := self._lacks(request, "pull_requests", "write")) is not None:
            return denied
        repo = f"{request.params['owner']}/{request.params['repo']}"
        query = request.query
        state = query.get("state", "open")
        head = query.get("head")
        found = [
            pull
            for pull in self.pulls
            if pull["_repo"] == repo
            and (state == "all" or pull["state"] == state)
            and (head is None or pull["head"]["label"] == head)
        ]
        page, link = _page(request, self.page_cap, [_public(pull) for pull in found])
        return json_response(200, page, link)


def _public(obj: dict[str, Any]) -> dict[str, Any]:
    """The object as GitHub shows it: our bookkeeping keys (``_repo``, ``_pr``) stay hidden."""
    return {key: value for key, value in obj.items() if not key.startswith("_")}


def _page(request: RecordedRequest, cap: int, items: list[Any]) -> tuple[list[Any], dict[str, str]]:
    """One page of ``items`` and its ``Link`` header (``next`` only when there is another page)."""
    query = request.query
    per_page = max(1, min(int(query.get("per_page", 30)), cap))
    number = max(1, int(query.get("page", 1)))
    chunk = items[(number - 1) * per_page : number * per_page]
    if number * per_page >= len(items):
        return chunk, {}
    following = {**query, "page": str(number + 1)}
    url = f"https://{API_HOST}{request.path}?" + "&".join(
        f"{key}={quote(value, safe='')}" for key, value in following.items()
    )
    return chunk, {"Link": f'<{url}>; rel="next"'}


# === Jobs, job logs and blobs (P1c b2, D-71) ==================================================
# Everything for the log source lives in this section; the routes are added with ``add_route``
# by ``GitHubLogsFake(github)``, so the other route families do not touch it.

BLOB_HOST = "productionresultssa7.blob.core.windows.net"
JOBS_TEMPLATE = "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
LOG_TEMPLATE = "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
BLOB_TEMPLATE = "/actions-results/{blob_id}/workflow-job-run-{run_uuid}/logs/job/job-logs.txt"
BLOB_CHUNK = 64 * 1024


def signed_query(n: int) -> str:
    """The shape of an Azure SAS query. Each redirect gets a new signature."""
    return f"sv=2023-11-03&se=2026-11-12T12%3A10%3A00Z&sp=r&sig=fake-signature-{n:03d}"


class _Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


@dataclass
class BlobState:
    """One log as the blob host serves it. ``body`` is what travels (already compressed for a
    ``Content-Encoding``); ``stream`` replaces it with chunks of unknown total length."""

    body: bytes = b""
    stream: Callable[[], AsyncIterator[bytes]] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    ignore_range: bool = False  # answer 200 with everything, as Azure does for ``bytes=-N``
    content_length: bool = True
    served: int = 0  # bytes handed to the client so far


def jobs_json(
    job_ids: list[int], *, run_attempt: int = 1, conclusion: str = "failure"
) -> list[Any]:
    """Jobs in the shape of the jobs API (the fields ``jobs.py`` reads, plus ``run_attempt``)."""
    return [
        {
            "id": job_id,
            "name": f"job-{job_id}",
            "run_attempt": run_attempt,
            "conclusion": conclusion,
            "started_at": "2026-11-12T12:00:00Z",
            "completed_at": "2026-11-12T12:05:00Z",
            "labels": ["ubuntu-latest"],
            "steps": [
                {
                    "number": 1,
                    "name": "Run tests",
                    "conclusion": conclusion,
                    "started_at": "2026-11-12T12:01:00Z",
                    "completed_at": "2026-11-12T12:04:00Z",
                }
            ],
        }
        for job_id in job_ids
    ]


class GitHubLogsFake:
    """The jobs listing, the log endpoint (302) and the blob host, on top of ``FakeGitHub``."""

    def __init__(self, github: FakeGitHub) -> None:
        self.github = github
        self.blobs: dict[int, BlobState] = {}  # by job id
        self.redirect_host = BLOB_HOST
        self.redirect_scheme = "https"
        self._jobs: dict[tuple[int, int], tuple[list[Any], int]] = {}
        self._blob_ids: dict[str, int] = {}  # blob id in the URL -> job id
        self._redirects = 0
        github.add_route("GET", JOBS_TEMPLATE, self._list_jobs)
        github.add_route("GET", LOG_TEMPLATE, self._log_redirect)
        github.add_route("GET", BLOB_TEMPLATE, self._blob, auth="none")

    def set_jobs(
        self, run_id: int, attempt: int, jobs: list[Any], *, page_size: int | None = None
    ) -> None:
        """The jobs of an attempt; ``page_size`` overrides ``per_page`` to force several pages."""
        self._jobs[(run_id, attempt)] = (jobs, page_size or 0)

    def set_log(self, job_id: int, body: bytes | None = None, **options: Any) -> BlobState:
        state = BlobState(body=body or b"", **options)
        self.blobs[job_id] = state
        return state

    # --- routes --------------------------------------------------------------------------

    def _list_jobs(self, request: RecordedRequest) -> httpx2.Response:
        key = (int(request.params["run_id"]), int(request.params["attempt"]))
        if key not in self._jobs:
            return error_response(404, "Not Found")
        jobs, forced = self._jobs[key]
        query = request.query
        size = forced or int(query.get("per_page", "30"))
        page = int(query.get("page", "1"))
        chunk = jobs[(page - 1) * size : page * size]
        headers: dict[str, str] = {}
        if page * size < len(jobs):
            base = f"https://{API_HOST}{request.path}"
            headers["Link"] = f'<{base}?per_page={size}&page={page + 1}>; rel="next"'
        return json_response(200, {"total_count": len(jobs), "jobs": chunk}, headers)

    def _log_redirect(self, request: RecordedRequest) -> httpx2.Response:
        job_id = int(request.params["job_id"])
        if job_id not in self.blobs:
            return error_response(404, "Not Found")
        blob_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"blob-{job_id}"))
        self._blob_ids[blob_id] = job_id
        self._redirects += 1
        path = BLOB_TEMPLATE.format(blob_id=blob_id, run_uuid=blob_id)
        location = (
            f"{self.redirect_scheme}://{self.redirect_host}{path}?{signed_query(self._redirects)}"
        )
        return httpx2.Response(302, headers={"Location": location})

    def _blob(self, request: RecordedRequest) -> httpx2.Response:
        job_id = self._blob_ids.get(request.params["blob_id"])
        if request.host == API_HOST or job_id is None or "sig" not in request.query:
            return error_response(404, "Not Found")
        state = self.blobs[job_id]
        headers = {"Content-Type": "text/plain", "Accept-Ranges": "bytes", **state.headers}
        body = state.body
        status = 200
        range_header = request.header("range")
        match = re.fullmatch(r"bytes=(\d+)-", range_header or "")
        if match and not state.ignore_range and state.stream is None:  # a suffix is ignored
            start = int(match.group(1))
            status = 206
            headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
            body = body[start:]
        if state.stream is None and state.content_length:
            headers["Content-Length"] = str(len(body))
        return httpx2.Response(status, headers=headers, stream=_Stream(self._chunks(state, body)))

    async def _chunks(self, state: BlobState, body: bytes) -> AsyncIterator[bytes]:
        if state.stream is not None:
            async for chunk in state.stream():
                state.served += len(chunk)
                yield chunk
            return
        for start in range(0, len(body), BLOB_CHUNK):
            chunk = body[start : start + BLOB_CHUNK]
            state.served += len(chunk)
            yield chunk
