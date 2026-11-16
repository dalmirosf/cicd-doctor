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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote

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
