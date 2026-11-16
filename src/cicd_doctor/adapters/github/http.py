"""The GitHub REST client: headers, timeouts, pagination and the one place that maps errors (D-67).

``GitHubHttp`` is a thin layer over ``httpx2.AsyncClient``. It knows nothing about endpoints: the
adapters built on it (log source, reporter, pull requests) name the endpoint and read the JSON.

- Every endpoint is a *template* (``/repos/{owner}/{repo}/actions/jobs/{job_id}``) plus the values
  for its placeholders. The values are percent-encoded into the URL, and only the template goes
  into an error message: a message never carries a value, a URL query, a token or the body of a
  response (a body can echo anything, and a signed URL carries its signature in the query).
- Redirects are never followed. A caller that expects one (the job log endpoint answers 302 with
  the blob URL) asks for it with ``allow_redirect`` and reads ``Location`` itself.
- There are no retries here, with one exception: a 401 asks the credential to refresh and sends the
  request once more. Every other retry belongs to the job (D-23).
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal, Protocol, Self
from urllib.parse import quote

import httpx2

from cicd_doctor.core.errors import PermanentError, TransientError
from cicd_doctor.core.ports import Clock

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"  # the version the spike saw selected (SPEC §4.2)
USER_AGENT = "cicd-doctor"
TIMEOUT = httpx2.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

PER_PAGE = 100
DEFAULT_MAX_PAGES = 10

# What GitHub documents for a secondary rate limit that comes without ``retry-after``.
SECONDARY_LIMIT_WAIT = timedelta(seconds=60)
MIN_RATE_LIMIT_WAIT = timedelta(seconds=1)
# The primary window is one hour: a longer wait can only come from a bad header.
MAX_RATE_LIMIT_WAIT = timedelta(hours=1)

Method = Literal["GET", "POST", "PATCH"]
Query = Mapping[str, str | int]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SECONDS = re.compile(r"[0-9]{1,12}")
# ``x-accepted-github-permissions`` is a GitHub header, not a body. It is copied into the message
# of a 403 only when it has this shape (for example ``secrets=read``).
_ACCEPTED_PERMISSIONS = re.compile(r"[a-z_=,; ]{1,100}")


class GitHubTransientError(TransientError):
    """A ``TransientError`` that also says which status GitHub answered (``None``: no answer)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message, retry_after=retry_after)
        self.status_code = status_code


class GitHubPermanentError(PermanentError):
    """A ``PermanentError`` that also says which status GitHub answered, so callers can tell a 404
    (logs expired) from a 403 (permission) without parsing the message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Credential(Protocol):
    """What ``GitHubHttp`` needs to authenticate a request, and to recover from a 401."""

    async def authorization(self) -> str:
        """The value of the ``Authorization`` header for the next request."""
        ...

    async def refresh(self, rejected: str) -> None:
        """GitHub answered 401 to ``rejected``: make the next ``authorization()`` a new one."""
        ...


@dataclass(frozen=True)
class Paginated:
    """The items of a paginated listing; ``truncated`` is true when the page cap stopped it."""

    items: list[Any]
    truncated: bool


class GitHubHttp:
    def __init__(
        self,
        clock: Clock,
        *,
        base_url: str = API_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._base = httpx2.URL(base_url)
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def request(
        self,
        method: Method,
        template: str,
        *,
        path: Mapping[str, str | int] | None = None,
        query: Query | None = None,
        json: Any = None,
        credential: Credential | None = None,
        allow_redirect: bool = False,
    ) -> httpx2.Response:
        """One request. Returns the response for any status below 400 (a 3xx only with
        ``allow_redirect``) and raises ``GitHubTransientError`` or ``GitHubPermanentError``
        otherwise."""
        return await self._send(
            method,
            template,
            _format_path(template, path),
            query=query,
            json=json,
            credential=credential,
            allow_redirect=allow_redirect,
        )

    async def paginate(
        self,
        template: str,
        *,
        path: Mapping[str, str | int] | None = None,
        query: Query | None = None,
        credential: Credential | None = None,
        items_key: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> Paginated:
        """Every item of a ``GET`` listing, following ``Link: rel="next"`` up to ``max_pages``.

        ``items_key`` names the array inside an object body (the jobs API answers
        ``{"total_count": 2, "jobs": [...]}``); without it the body is the array itself.
        """
        target: str | httpx2.URL = _format_path(template, path)
        params: Query | None = {**(query or {}), "per_page": PER_PAGE}
        items: list[Any] = []
        for _ in range(max_pages):
            response = await self._send(
                "GET",
                template,
                target,
                query=params,
                json=None,
                credential=credential,
                allow_redirect=False,
            )
            items.extend(_page_items(response, template, items_key))
            next_url = response.links.get("next", {}).get("url")
            if next_url is None:
                return Paginated(items, truncated=False)
            target = self._same_origin(next_url, template, response.status_code)
            params = None  # the next link already carries its own query
        logger.warning("GET %s: stopped after the cap of %d pages", template, max_pages)
        return Paginated(items, truncated=True)

    # --- internals -------------------------------------------------------------------------

    async def _send(
        self,
        method: Method,
        template: str,
        target: str | httpx2.URL,
        *,
        query: Query | None,
        json: Any,
        credential: Credential | None,
        allow_redirect: bool,
    ) -> httpx2.Response:
        response, authorization = await self._once(
            method, template, target, query, json, credential
        )
        if response.status_code == 401 and credential is not None and authorization is not None:
            await credential.refresh(authorization)
            response, _ = await self._once(method, template, target, query, json, credential)
        self._raise_for_status(method, template, response, allow_redirect)
        return response

    async def _once(
        self,
        method: Method,
        template: str,
        target: str | httpx2.URL,
        query: Query | None,
        json: Any,
        credential: Credential | None,
    ) -> tuple[httpx2.Response, str | None]:
        authorization = await credential.authorization() if credential is not None else None
        headers = {"Authorization": authorization} if authorization is not None else None
        try:
            response = await self._client.request(
                method, target, params=query, json=json, headers=headers
            )
        except httpx2.TimeoutException as exc:
            raise GitHubTransientError(f"{method} {template}: timed out") from exc
        except httpx2.TransportError as exc:
            raise GitHubTransientError(
                f"{method} {template}: connection error ({type(exc).__name__})"
            ) from exc
        return response, authorization

    def _raise_for_status(
        self, method: Method, template: str, response: httpx2.Response, allow_redirect: bool
    ) -> None:
        status = response.status_code
        if status < 300:
            return
        label = f"{method} {template} answered {status}"
        if status < 400:
            if allow_redirect:
                return
            raise GitHubPermanentError(f"{label}: unexpected redirect", status_code=status)
        if status == 401:  # still 401 after the refresh
            raise GitHubTransientError(f"{label}: the credential was rejected", status_code=status)
        if status in (403, 429):
            wait = self._rate_limit_wait(response)
            if wait is not None:
                raise GitHubTransientError(
                    f"{label}: rate limited", status_code=status, retry_after=wait
                )
            needed = _accepted_permissions(response) if status == 403 else None
            suffix = f" (the app needs: {needed})" if needed else ""
            raise GitHubPermanentError(label + suffix, status_code=status)
        if status >= 500 or status == 408:
            raise GitHubTransientError(label, status_code=status)
        raise GitHubPermanentError(label, status_code=status)  # 404, 410, 422 and the rest

    def _rate_limit_wait(self, response: httpx2.Response) -> timedelta | None:
        """How long to wait when a 403 or 429 is a rate limit, ``None`` when it is not one.

        The order is the one GitHub documents: ``retry-after`` first, then an exhausted primary
        limit (wait for ``x-ratelimit-reset``), then a secondary limit without any header.
        """
        headers = response.headers
        retry_after = _seconds(headers.get("retry-after"))
        if retry_after is not None:
            return _clamp(timedelta(seconds=retry_after))
        if headers.get("x-ratelimit-remaining") == "0":
            reset = _seconds(headers.get("x-ratelimit-reset"))
            if reset is None:
                return SECONDARY_LIMIT_WAIT
            return _clamp(datetime.fromtimestamp(reset, UTC) - self._clock.now())
        if response.status_code == 429 or _body_says_rate_limit(response):
            return SECONDARY_LIMIT_WAIT
        return None

    def _same_origin(self, url: str, template: str, status: int) -> httpx2.URL:
        """The ``next`` link, only if it stays on the API host: the token goes with it."""
        try:
            parsed = httpx2.URL(url)
        except httpx2.InvalidURL:
            parsed = None
        if (
            parsed is None
            or parsed.scheme != self._base.scheme
            or parsed.host != self._base.host
            or parsed.port != self._base.port
        ):
            raise GitHubPermanentError(
                f"GET {template}: the next page link points to another origin", status_code=status
            )
        return parsed


def _format_path(template: str, values: Mapping[str, str | int] | None) -> str:
    """Fill the placeholders of ``template`` with percent-encoded values."""
    supplied = values or {}

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in supplied:
            raise ValueError(f"{template}: no value for {{{name}}}")
        raw = str(supplied[name])
        if raw in ("", ".", ".."):  # an empty or dot segment would change the path
            raise ValueError(f"{template}: {{{name}}} is not a valid path segment")
        return quote(raw, safe="")

    return _PLACEHOLDER.sub(fill, template)


def _seconds(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    return int(text) if _SECONDS.fullmatch(text) else None


def _clamp(wait: timedelta) -> timedelta:
    return min(max(wait, MIN_RATE_LIMIT_WAIT), MAX_RATE_LIMIT_WAIT)


def _body_says_rate_limit(response: httpx2.Response) -> bool:
    """A 403 without headers is a secondary rate limit when its message says so. The message is
    only read to decide: it is never copied anywhere."""
    try:
        body = response.json()
    except ValueError:
        return False
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return "rate limit" in lowered or "abuse detection" in lowered


def _accepted_permissions(response: httpx2.Response) -> str | None:
    value = response.headers.get("x-accepted-github-permissions")
    if value is not None and _ACCEPTED_PERMISSIONS.fullmatch(value):
        return value
    return None


def _page_items(response: httpx2.Response, template: str, items_key: str | None) -> list[Any]:
    try:
        body = response.json()
    except ValueError:
        raise GitHubTransientError(
            f"GET {template}: the response is not JSON", status_code=response.status_code
        ) from None
    items = body.get(items_key) if items_key is not None and isinstance(body, dict) else body
    if not isinstance(items, list):
        raise GitHubTransientError(
            f"GET {template}: the response has an unexpected shape",
            status_code=response.status_code,
        )
    return items
