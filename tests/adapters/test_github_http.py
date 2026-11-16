"""The GitHub HTTP client (D-67): headers, no redirects, pagination, one error mapping.

Everything runs against ``FakeGitHub`` over ``httpx2.MockTransport``: no network. Every error the
fake answers carries ``CANARY`` in its body, so each mapping test also shows that a response body
never reaches an exception message.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta

import httpx2
import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github import http as github_http
from cicd_doctor.adapters.github.http import (
    DEFAULT_MAX_PAGES,
    GitHubHttp,
    GitHubPermanentError,
    GitHubTransientError,
)
from cicd_doctor.app.container import HTTP_LOGGERS, configure_logging
from cicd_doctor.core.errors import PermanentError, TransientError
from tests.github_fake import CANARY, FakeGitHub, RecordedRequest, json_response

SIGNATURE = "c2ln-not-a-real-signature"
SIGNED_QUERY = f"sig={SIGNATURE}&se=2026-01-01T00%3A10%3A00Z"


def ok(request: RecordedRequest) -> httpx2.Response:
    return json_response(200, {"ok": True})


@pytest.fixture(autouse=True)
def _http_loggers_as_the_app_sets_them() -> Iterator[None]:
    configure_logging()
    yield
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _things(github: FakeGitHub) -> None:
    github.add_route("GET", "/things", ok, auth="none")


class StubCredential:
    """Hands out ``tokens`` in order, moving to the next one after each refresh."""

    def __init__(self, *tokens: str) -> None:
        self._tokens = list(tokens)
        self.rejected: list[str] = []

    async def authorization(self) -> str:
        return f"Bearer {self._tokens[0]}"

    async def refresh(self, rejected: str) -> None:
        self.rejected.append(rejected)
        if len(self._tokens) > 1:  # the last token stays, however often it is rejected
            self._tokens.pop(0)


# --- requests ------------------------------------------------------------------------------


async def test_sends_the_api_headers_and_no_credential_by_default(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    await http.request("GET", "/things")

    sent = github.requests[0]
    assert sent.host == "api.github.com"
    assert sent.header("accept") == "application/vnd.github+json"
    assert sent.header("x-github-api-version") == "2022-11-28"
    assert sent.header("user-agent") == "cicd-doctor"
    assert not sent.has_header("authorization")


async def test_sends_the_credential_it_is_given(http: GitHubHttp, github: FakeGitHub) -> None:
    await http.request("GET", "/things", credential=StubCredential("tok-1"))

    assert github.requests[0].has_bearer("tok-1")


def test_timeouts_are_5s_to_connect_and_30s_to_read(http: GitHubHttp) -> None:
    assert github_http.TIMEOUT.connect == 5.0
    assert github_http.TIMEOUT.read == 30.0
    assert http._client.timeout == github_http.TIMEOUT


async def test_method_body_query_and_percent_encoded_path_reach_the_server(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    template = "/repos/{owner}/{repo}/check-runs/{check_run_id}"
    github.add_route("PATCH", template, ok, auth="none")

    await http.request(
        "PATCH",
        template,
        path={"owner": "acme", "repo": "a b/c", "check_run_id": 7},
        query={"note": "x y"},
        json={"status": "completed"},
    )

    sent = github.requests[0]
    assert (sent.method, sent.path) == ("PATCH", "/repos/acme/a%20b%2Fc/check-runs/7")
    assert sent.params == {"owner": "acme", "repo": "a b/c", "check_run_id": "7"}
    assert sent.query == {"note": "x y"}
    assert sent.json == {"status": "completed"}


@pytest.mark.parametrize("value", ["", ".", ".."])
async def test_a_value_that_would_change_the_path_is_refused(
    http: GitHubHttp, github: FakeGitHub, value: str
) -> None:
    with pytest.raises(ValueError, match="not a valid path segment"):
        await http.request("GET", "/repos/{owner}/things", path={"owner": value})

    assert github.requests == []


async def test_a_placeholder_without_a_value_is_refused(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    with pytest.raises(ValueError, match="no value for"):
        await http.request("GET", "/repos/{owner}/things", path={})

    assert github.requests == []


# --- redirects -------------------------------------------------------------------------------


def redirect(request: RecordedRequest) -> httpx2.Response:
    return httpx2.Response(302, headers={"location": f"https://blob.test/log.txt?{SIGNED_QUERY}"})


async def test_a_redirect_is_returned_and_not_followed_when_the_caller_expects_it(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.add_route("GET", "/logs", redirect, auth="none")

    response = await http.request("GET", "/logs", allow_redirect=True)

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://blob.test/log.txt")
    assert [r.host for r in github.requests] == ["api.github.com"]  # the blob was not requested


async def test_an_unexpected_redirect_is_permanent_and_does_not_name_the_target(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.add_route("GET", "/logs", redirect, auth="none")

    with pytest.raises(GitHubPermanentError) as caught:
        await http.request("GET", "/logs")

    assert str(caught.value) == "GET /logs answered 302: unexpected redirect"
    assert caught.value.status_code == 302
    assert [r.host for r in github.requests] == ["api.github.com"]


# --- error mapping ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, PermanentError),
        (404, PermanentError),
        (409, PermanentError),
        (410, PermanentError),
        (422, PermanentError),
        (451, PermanentError),
        (408, TransientError),
        (500, TransientError),
        (502, TransientError),
        (503, TransientError),
        (504, TransientError),
    ],
)
async def test_error_mapping_and_no_retry(
    http: GitHubHttp, github: FakeGitHub, status: int, kind: type[Exception]
) -> None:
    github.fail("GET", "/things", status)

    with pytest.raises(kind) as caught:
        await http.request("GET", "/things", query={"sig": SIGNATURE})

    assert getattr(caught.value, "status_code", None) == status
    assert str(caught.value) == f"GET /things answered {status}"  # no query, no body
    assert len(github.requests) == 1  # the job retries, not the client


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ReadTimeout(f"read timed out for https://x.test/?{SIGNED_QUERY}"),
        httpx2.ConnectTimeout("connect timed out"),
        httpx2.ConnectError(f"cannot connect to https://x.test/?{SIGNED_QUERY}"),
        httpx2.RemoteProtocolError("server disconnected"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_timeouts_and_connection_errors_are_transient_without_their_text(
    http: GitHubHttp, github: FakeGitHub, error: httpx2.TransportError
) -> None:
    github.fail("GET", "/things", error=error)

    with pytest.raises(GitHubTransientError) as caught:
        await http.request("GET", "/things")

    assert caught.value.status_code is None
    assert str(caught.value).startswith("GET /things: ")
    assert "sig=" not in str(caught.value)
    assert "x.test" not in str(caught.value)
    assert len(github.requests) == 1


async def test_a_403_that_is_a_permission_names_the_permission_github_reports(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.fail(
        "GET",
        "/things",
        403,
        headers={"x-accepted-github-permissions": "secrets=read", "x-ratelimit-remaining": "4998"},
        message="Resource not accessible by integration",
    )

    with pytest.raises(GitHubPermanentError) as caught:
        await http.request("GET", "/things")

    assert str(caught.value) == "GET /things answered 403 (the app needs: secrets=read)"
    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    "value",
    ["Secrets=Read", "secrets=read\n<b>x</b>", "x" * 101, "", "<script>", "a=read, b=write; c=1"],
    ids=["upper-case", "newline", "too-long", "empty", "markup", "digit"],
)
async def test_a_permissions_header_of_another_shape_is_not_copied(
    http: GitHubHttp, github: FakeGitHub, value: str
) -> None:
    github.fail("GET", "/things", 403, headers={"x-accepted-github-permissions": value})

    with pytest.raises(GitHubPermanentError) as caught:
        await http.request("GET", "/things")

    assert str(caught.value) == "GET /things answered 403"


async def test_the_permissions_header_is_only_copied_from_a_403(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.fail("GET", "/things", 404, headers={"x-accepted-github-permissions": "actions=read"})

    with pytest.raises(GitHubPermanentError) as caught:
        await http.request("GET", "/things")

    assert str(caught.value) == "GET /things answered 404"


# --- rate limits -----------------------------------------------------------------------------

SECONDARY = "You have exceeded a secondary rate limit. Please wait a few minutes."


@pytest.mark.parametrize(
    ("status", "headers", "message", "expected"),
    [
        # primary limit: wait until x-ratelimit-reset
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "+120"}, "API rate limit", 120),
        (429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "+45"}, "slow down", 45),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "-30"}, "API rate limit", 1),
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "+86400"},
            "API rate limit",
            3600,
        ),
        (403, {"x-ratelimit-remaining": "0"}, "API rate limit", 60),
        # secondary limit with retry-after: honoured (also over a primary one)
        (429, {"retry-after": "37"}, "slow down", 37),
        (403, {"retry-after": "37"}, SECONDARY, 37),
        (
            403,
            {"retry-after": "37", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "+500"},
            SECONDARY,
            37,
        ),
        (403, {"retry-after": "0"}, SECONDARY, 1),
        # secondary limit without a header: the minute GitHub documents
        (403, {}, SECONDARY, 60),
        (403, {}, "You have triggered an abuse detection mechanism", 60),
        (429, {}, "slow down", 60),
        (429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, "slow down", 60),
    ],
    ids=[
        "primary-reset",
        "primary-on-429",
        "reset-in-the-past",
        "reset-capped",
        "primary-without-reset",
        "retry-after-on-429",
        "retry-after-on-403",
        "retry-after-wins",
        "retry-after-zero",
        "secondary-no-header",
        "abuse-detection",
        "429-no-header",
        "retry-after-as-a-date",
    ],
)
async def test_rate_limits_are_transient_with_the_wait_github_asks_for(
    http: GitHubHttp,
    github: FakeGitHub,
    clock: FakeClock,
    status: int,
    headers: dict[str, str],
    message: str,
    expected: int,
) -> None:
    now = int(clock.now().timestamp())
    resolved = {
        name: str(now + int(value)) if name == "x-ratelimit-reset" else value
        for name, value in headers.items()
    }
    github.fail("GET", "/things", status, headers=resolved, message=message)

    with pytest.raises(GitHubTransientError) as caught:
        await http.request("GET", "/things")

    assert caught.value.retry_after == timedelta(seconds=expected)
    assert caught.value.status_code == status
    assert str(caught.value) == f"GET /things answered {status}: rate limited"
    assert len(github.requests) == 1


async def test_a_403_with_requests_left_and_no_rate_limit_message_is_a_permission(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.fail(
        "GET",
        "/things",
        403,
        headers={"x-ratelimit-remaining": "4999"},
        message="Resource not accessible by integration",
    )

    with pytest.raises(GitHubPermanentError):
        await http.request("GET", "/things")


# --- 401: refresh once ------------------------------------------------------------------------


async def test_a_401_refreshes_the_credential_and_sends_the_request_once_more(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.fail("GET", "/things", 401, message="Bad credentials")
    credential = StubCredential("stale", "fresh")

    response = await http.request("GET", "/things", credential=credential)

    assert response.status_code == 200
    assert credential.rejected == ["Bearer stale"]  # it is told which one failed
    assert [r.has_bearer("stale") for r in github.requests] == [True, False]
    assert github.requests[1].has_bearer("fresh")


async def test_a_second_401_is_transient(http: GitHubHttp, github: FakeGitHub) -> None:
    github.fail("GET", "/things", 401, times=2)
    credential = StubCredential("stale", "fresh", "never-used")

    with pytest.raises(GitHubTransientError) as caught:
        await http.request("GET", "/things", credential=credential)

    assert caught.value.status_code == 401
    assert str(caught.value) == "GET /things answered 401: the credential was rejected"
    assert len(github.requests) == 2  # one refresh, not a loop
    assert len(credential.rejected) == 1


async def test_a_401_without_a_credential_is_transient_and_not_retried(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    github.fail("GET", "/things", 401)

    with pytest.raises(GitHubTransientError):
        await http.request("GET", "/things")

    assert len(github.requests) == 1


# --- pagination -------------------------------------------------------------------------------


def register_listing(github: FakeGitHub, pages: int, items_key: str | None = None) -> None:
    """``GET /items``: two items per page, a ``Link: rel="next"`` header until the last page."""

    def handler(request: RecordedRequest) -> httpx2.Response:
        page = int(request.query.get("page", "1"))
        items = [{"page": page, "n": n} for n in range(2)]
        links = {}
        if page < pages:
            url = f"https://api.github.com/items?per_page=100&page={page + 1}"
            links["link"] = f'<{url}>; rel="next", <{url}>; rel="last"'
        body = {"total_count": 2 * pages, items_key: items} if items_key else items
        return json_response(200, body, links)

    github.add_route("GET", "/items", handler, auth="none")


async def test_paginates_three_pages_by_the_link_header(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    register_listing(github, pages=3)

    result = await http.paginate("/items", query={"filter": "latest"})

    assert [item["page"] for item in result.items] == [1, 1, 2, 2, 3, 3]
    assert result.truncated is False
    assert [r.query for r in github.requests] == [
        {"filter": "latest", "per_page": "100"},
        {"per_page": "100", "page": "2"},
        {"per_page": "100", "page": "3"},
    ]


async def test_reads_the_items_of_an_object_body_by_key(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    register_listing(github, pages=2, items_key="jobs")

    result = await http.paginate("/items", items_key="jobs")

    assert len(result.items) == 4


async def test_stops_at_the_page_cap_and_says_so(
    http: GitHubHttp, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    register_listing(github, pages=5)

    with caplog.at_level(logging.WARNING):
        result = await http.paginate("/items", max_pages=2)

    assert len(github.requests) == 2
    assert len(result.items) == 4
    assert result.truncated is True
    assert "GET /items: stopped after the cap of 2 pages" in caplog.text


async def test_a_listing_that_ends_exactly_at_the_cap_is_not_truncated(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    register_listing(github, pages=2)

    result = await http.paginate("/items", max_pages=2)

    assert result.truncated is False
    assert len(github.requests) == 2


def test_the_default_page_cap_is_ten() -> None:
    assert DEFAULT_MAX_PAGES == 10


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.test/items?page=2",
        "http://api.github.com/items?page=2",
        "https://api.github.com:8443/items?page=2",
        "/items?page=2",
    ],
    ids=["other-host", "downgraded-scheme", "other-port", "relative"],
)
async def test_a_next_link_to_another_origin_is_never_followed(
    http: GitHubHttp, github: FakeGitHub, next_url: str
) -> None:
    def handler(request: RecordedRequest) -> httpx2.Response:
        return json_response(200, [{"n": 1}], {"link": f'<{next_url}>; rel="next"'})

    github.add_route("GET", "/items", handler, auth="none")

    with pytest.raises(GitHubPermanentError, match="another origin"):
        await http.paginate("/items", credential=StubCredential("tok-1"))

    assert len(github.requests) == 1  # the token went nowhere else


async def test_an_error_on_a_later_page_raises_instead_of_returning_a_partial_list(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    def second_page_fails(request: RecordedRequest) -> httpx2.Response:
        if request.query.get("page") == "2":
            return json_response(502, {"message": CANARY})
        url = "https://api.github.com/items?per_page=100&page=2"
        return json_response(200, [{"n": 1}], {"link": f'<{url}>; rel="next"'})

    github.add_route("GET", "/items", second_page_fails, auth="none")

    with pytest.raises(GitHubTransientError, match="GET /items answered 502"):
        await http.paginate("/items")

    assert len(github.requests) == 2


@pytest.mark.parametrize(
    "body",
    [httpx2.Response(200, text=f"<html>{CANARY}</html>"), httpx2.Response(200, json={"x": 1})],
    ids=["not-json", "not-a-list"],
)
async def test_a_body_that_is_not_a_listing_is_transient_and_never_quoted(
    http: GitHubHttp, github: FakeGitHub, body: httpx2.Response
) -> None:
    github.add_route("GET", "/items", lambda request: body, auth="none")

    with pytest.raises(GitHubTransientError) as caught:
        await http.paginate("/items")

    assert CANARY not in str(caught.value)
    assert str(caught.value).startswith("GET /items: the response ")


# --- nothing sensitive in messages or logs ----------------------------------------------------


async def test_no_message_or_log_carries_a_token_a_query_or_a_response_body(
    http: GitHubHttp, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = "tok-secret-1"
    texts: list[str] = []

    async def attempt(status: int, headers: dict[str, str] | None = None) -> None:
        # a 401 is retried once, so it has to fail twice to surface
        github.fail("GET", "/things", status, headers=headers, times=2 if status == 401 else 1)
        with pytest.raises((TransientError, PermanentError)) as caught:
            await http.request(
                "GET", "/things", query={"sig": SIGNATURE}, credential=StubCredential(token)
            )
        texts.extend([str(caught.value), repr(caught.value)])

    for status in (401, 403, 404, 410, 422, 429, 500, 503):
        await attempt(status)
    await attempt(403, {"x-accepted-github-permissions": "secrets=read"})
    await attempt(429, {"retry-after": "5"})
    texts.append(caplog.text)

    for secret in (token, SIGNATURE, CANARY):
        assert not any(secret in text for text in texts), f"{len(secret)}-character secret leaked"


async def test_httpx2_would_log_the_url_of_every_request_without_configure_logging(
    http: GitHubHttp, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    # What configure_logging() exists for: at INFO the client prints the whole URL.
    logging.getLogger("httpx2").setLevel(logging.INFO)
    await http.request("GET", "/things", query={"sig": SIGNATURE})
    assert SIGNATURE in caplog.text

    caplog.clear()
    configure_logging()
    await http.request("GET", "/things", query={"sig": SIGNATURE})
    assert caplog.text == ""


def test_configure_logging_silences_httpx2_and_httpcore2_children() -> None:
    configure_logging()

    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore2.http11").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore2.connection").getEffectiveLevel() == logging.WARNING
