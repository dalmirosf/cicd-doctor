"""GitHub App authentication (D-68): the app JWT, scoped installation tokens and their cache.

The key pair is generated per test session (``app_key``): no PEM is ever committed. ``FakeGitHub``
checks the JWT the way GitHub does and issues the installation tokens, so the adapters run the
real flow end to end, offline.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import (
    DIAGNOSIS_PERMISSIONS,
    AppIdentity,
    GitHubAppAuth,
    load_private_key,
)
from cicd_doctor.adapters.github.http import (
    GitHubHttp,
    GitHubPermanentError,
    GitHubTransientError,
)
from cicd_doctor.app.container import HTTP_LOGGERS, configure_logging
from cicd_doctor.core.errors import PermanentError
from tests.github_fake import (
    APP_ID,
    APP_SLUG,
    CANARY,
    INSTALLATION_ID,
    REPO_ID,
    FakeGitHub,
    RecordedRequest,
    json_response,
)

TOKEN_PATH = "/app/installations/{installation_id}/access_tokens"
PING = "/repos/{owner}/{repo}/ping"
OTHER_REPO_ID = 3003


def pem_of(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    fmt: serialization.PrivateFormat = serialization.PrivateFormat.PKCS8,
    encryption: serialization.KeySerializationEncryption | None = None,
) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, fmt, encryption or serialization.NoEncryption()
    ).decode()


@pytest.fixture(autouse=True)
def _http_loggers_as_the_app_sets_them() -> Iterator[None]:
    configure_logging()
    yield
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _ping(github: FakeGitHub) -> None:
    """``GET /repos/{owner}/{repo}/ping``: answers 200 to a valid installation token."""

    def ping(request: RecordedRequest) -> httpx2.Response:
        return json_response(200, {"ok": True})

    github.add_route("GET", PING, ping)
    github.installations[INSTALLATION_ID].repo_ids.add(OTHER_REPO_ID)


async def ping(http: GitHubHttp, auth: GitHubAppAuth, repo_id: int = REPO_ID) -> None:
    credential = auth.installation_credential(INSTALLATION_ID, repo_id)
    await http.request(
        "GET", PING, path={"owner": "acme", "repo": "widgets"}, credential=credential
    )


def pings(github: FakeGitHub) -> list[RecordedRequest]:
    return github.requests_to("GET", PING)


def token_requests(github: FakeGitHub) -> list[RecordedRequest]:
    return github.requests_to("POST", TOKEN_PATH)


# --- the app JWT ---------------------------------------------------------------------------


def test_the_app_jwt_is_rs256_with_the_app_id_as_a_string_issuer(
    auth: GitHubAppAuth, clock: FakeClock, app_key: rsa.RSAPrivateKey
) -> None:
    token = auth.app_jwt()

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = (
        jwt.decode(  # the signature is verified; the dates are checked below, on the fake clock
            token,
            app_key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
    )
    now = int(clock.now().timestamp())
    assert claims == {"iss": str(APP_ID), "iat": now - 60, "exp": now + 9 * 60}
    assert isinstance(claims["iss"], str)


def test_the_app_jwt_follows_the_injected_clock(auth: GitHubAppAuth, clock: FakeClock) -> None:
    first = jwt.decode(auth.app_jwt(), options={"verify_signature": False})
    clock.advance(timedelta(minutes=3))
    second = jwt.decode(auth.app_jwt(), options={"verify_signature": False})

    assert second["exp"] - first["exp"] == 180
    assert second["iat"] - first["iat"] == 180


async def test_a_jwt_signed_with_another_key_is_not_accepted_by_github(
    clock: FakeClock, github: FakeGitHub, http: GitHubHttp
) -> None:
    """The fake enforces what GitHub does: a JWT signed with a stranger's key gets a 401."""
    stranger = GitHubAppAuth(
        http,
        app_id=APP_ID,
        private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        clock=clock,
    )

    with pytest.raises(GitHubTransientError) as caught:
        await stranger.app_identity()

    assert caught.value.status_code == 401


# --- loading the key -------------------------------------------------------------------------


def same_key(loaded: rsa.RSAPrivateKey, expected: rsa.RSAPrivateKey) -> bool:
    return loaded.public_key().public_numbers() == expected.public_key().public_numbers()


def test_loads_the_key_from_pem_content(app_key: rsa.RSAPrivateKey) -> None:
    assert same_key(load_private_key(SecretStr(pem_of(app_key))), app_key)


def test_loads_the_key_from_traditional_openssl_pem(app_key: rsa.RSAPrivateKey) -> None:
    pem = pem_of(app_key, serialization.PrivateFormat.TraditionalOpenSSL)

    assert "RSA PRIVATE KEY" in pem.splitlines()[0]  # the older format, not PKCS#8
    assert same_key(load_private_key(SecretStr(pem)), app_key)


def test_loads_the_key_from_a_one_line_value_with_escaped_newlines(
    app_key: rsa.RSAPrivateKey,
) -> None:
    one_line = pem_of(app_key).strip().replace("\n", "\\n")

    assert "\n" not in one_line
    assert same_key(load_private_key(SecretStr(one_line)), app_key)


def test_loads_the_key_from_a_file(app_key: rsa.RSAPrivateKey, tmp_path: Path) -> None:
    path = tmp_path / "app.pem"
    path.write_text(pem_of(app_key), encoding="utf-8")

    assert same_key(load_private_key(SecretStr(str(path))), app_key)


def test_a_value_that_is_neither_pem_nor_a_file_is_refused_without_echoing_it(
    tmp_path: Path,
) -> None:
    value = str(tmp_path / "missing-key-9f3a.pem")

    with pytest.raises(ValueError, match="GITHUB_PRIVATE_KEY is neither PEM content") as caught:
        load_private_key(SecretStr(value))

    assert "9f3a" not in str(caught.value)


def test_text_that_looks_like_a_pem_but_is_not_one_is_refused_without_echoing_it(
    app_key: rsa.RSAPrivateKey,
) -> None:
    first, *_, last = pem_of(app_key).splitlines()  # real BEGIN and END lines, a made-up body
    pem = "\n".join([first, "QUFBQS1ub3QtYS1rZXk=", last])

    with pytest.raises(ValueError, match="GITHUB_PRIVATE_KEY is not a valid") as caught:
        load_private_key(SecretStr(pem))

    assert "QUFBQS1u" not in str(caught.value)


def test_a_file_that_is_not_a_key_is_refused_without_its_content(tmp_path: Path) -> None:
    path = tmp_path / "garbage.pem"
    path.write_text("garbage-line-7d1c", encoding="utf-8")

    with pytest.raises(ValueError, match="GITHUB_PRIVATE_KEY is not a valid") as caught:
        load_private_key(SecretStr(str(path)))

    assert "7d1c" not in str(caught.value)


def test_an_encrypted_key_is_refused(app_key: rsa.RSAPrivateKey) -> None:
    encrypted = pem_of(app_key, encryption=serialization.BestAvailableEncryption(b"pw"))

    with pytest.raises(ValueError, match="not a valid unencrypted PEM"):
        load_private_key(SecretStr(encrypted))


def test_a_key_that_is_not_rsa_is_refused() -> None:
    elliptic = pem_of(ec.generate_private_key(ec.SECP256R1()))

    with pytest.raises(ValueError, match="must be an RSA private key"):
        load_private_key(SecretStr(elliptic))


def test_the_error_names_the_variable_it_was_given(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MY_KEY is neither"):
        load_private_key(SecretStr(str(tmp_path / "nope.pem")), variable="MY_KEY")


# --- installation tokens: scope ----------------------------------------------------------------


def test_the_diagnosis_permissions_are_the_three_the_use_case_needs() -> None:
    expected = {("actions", "read"), ("checks", "write"), ("pull_requests", "write")}

    assert expected == DIAGNOSIS_PERMISSIONS


async def test_the_token_is_requested_for_one_repo_with_only_the_use_case_permissions(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    await ping(http, auth)

    [request] = token_requests(github)
    assert request.params == {"installation_id": str(INSTALLATION_ID)}
    assert request.json == {
        "repository_ids": [REPO_ID],
        "permissions": {"actions": "read", "checks": "write", "pull_requests": "write"},
    }
    [issued] = github.issued
    assert issued.repo_ids == (REPO_ID,)
    assert "contents" not in issued.permissions  # the installation has it, the token does not
    assert pings(github)[0].has_bearer(issued.token)


async def test_a_use_case_with_other_permissions_gets_exactly_those(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    credential = auth.installation_credential(
        INSTALLATION_ID, REPO_ID, frozenset({("contents", "read"), ("metadata", "read")})
    )
    await http.request("GET", PING, path={"owner": "a", "repo": "b"}, credential=credential)

    assert token_requests(github)[0].json == {
        "repository_ids": [REPO_ID],
        "permissions": {"contents": "read", "metadata": "read"},
    }


async def test_the_token_request_is_signed_as_the_app(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    await ping(http, auth)

    authorization = token_requests(github)[0].header("authorization") or ""
    assert authorization.startswith("Bearer ")
    assert jwt.get_unverified_header(authorization.removeprefix("Bearer "))["alg"] == "RS256"
    assert not any(token_requests(github)[0].has_bearer(t.token) for t in github.issued)


# --- the cache ---------------------------------------------------------------------------------


async def test_a_token_is_reused_by_every_credential_with_the_same_key(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    for _ in range(3):
        await ping(http, auth)  # each call builds its own credential

    assert len(token_requests(github)) == 1
    assert len(pings(github)) == 3


async def test_another_repo_or_another_permission_set_gets_its_own_token(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    await ping(http, auth, REPO_ID)
    await ping(http, auth, OTHER_REPO_ID)
    other_permissions = auth.installation_credential(
        INSTALLATION_ID, REPO_ID, frozenset({("actions", "read")})
    )
    await http.request("GET", PING, path={"owner": "a", "repo": "b"}, credential=other_permissions)

    assert len(token_requests(github)) == 3
    assert [t.repo_ids for t in github.issued] == [(REPO_ID,), (OTHER_REPO_ID,), (REPO_ID,)]
    assert len({t.token for t in github.issued}) == 3


async def test_the_token_is_renewed_five_minutes_before_it_expires(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub, clock: FakeClock
) -> None:
    await ping(http, auth)  # valid for 60 minutes

    clock.advance(timedelta(minutes=54, seconds=59))
    await ping(http, auth)
    assert len(token_requests(github)) == 1  # one second before the margin

    clock.advance(timedelta(seconds=1))
    await ping(http, auth)
    assert len(token_requests(github)) == 2  # exactly 5 minutes before expires_at
    assert pings(github)[-1].has_bearer(github.issued[1].token)


async def test_expired_tokens_do_not_pile_up_in_the_cache(
    http: GitHubHttp, auth: GitHubAppAuth, clock: FakeClock
) -> None:
    for repo_id in (REPO_ID, OTHER_REPO_ID, REPO_ID):
        await ping(http, auth, repo_id)
        clock.advance(timedelta(hours=2))
    await ping(http, auth, OTHER_REPO_ID)

    assert len(vars(auth)["_cache"]) == 1  # the three before it had expired


# --- one request in flight per key --------------------------------------------------------------


async def test_ten_concurrent_requests_make_one_token_request(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.delay("POST", TOKEN_PATH, 0.05)  # long enough for all ten to be waiting

    await asyncio.gather(*(ping(http, auth) for _ in range(10)))

    assert len(token_requests(github)) == 1
    assert len(pings(github)) == 10
    assert all(r.has_bearer(github.issued[0].token) for r in pings(github))


async def test_concurrent_requests_for_different_repos_each_get_one_token(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.delay("POST", TOKEN_PATH, 0.05)

    await asyncio.gather(
        *(ping(http, auth, repo) for repo in (REPO_ID, OTHER_REPO_ID) for _ in range(5))
    )

    assert len(token_requests(github)) == 2


async def test_a_failed_token_request_is_shared_by_everyone_waiting_for_it(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.delay("POST", TOKEN_PATH, 0.05)
    del github.installations[INSTALLATION_ID]

    results = await asyncio.gather(*(ping(http, auth) for _ in range(10)), return_exceptions=True)

    assert all(isinstance(result, GitHubPermanentError) for result in results)
    assert len(token_requests(github)) == 1


async def test_cancelling_the_caller_that_started_the_request_does_not_cancel_the_others(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.delay("POST", TOKEN_PATH, 0.05)
    first = asyncio.ensure_future(ping(http, auth))
    second = asyncio.ensure_future(ping(http, auth))
    await asyncio.sleep(0.01)  # both are waiting for the token

    first.cancel()
    await second

    assert first.cancelled()
    assert len(token_requests(github)) == 1
    assert len(pings(github)) == 1


# --- a 401: new token, once ---------------------------------------------------------------------


async def test_a_401_drops_the_token_gets_a_new_one_and_retries_once(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    await ping(http, auth)
    github.revoke_all()  # GitHub no longer accepts the cached token

    await ping(http, auth)

    assert len(token_requests(github)) == 2
    first, second = github.issued
    assert [r.has_bearer(first.token) for r in pings(github)] == [True, True, False]
    assert pings(github)[2].has_bearer(second.token)


async def test_a_second_401_is_transient_and_gets_only_one_new_token(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.fail("GET", PING, 401, times=2)

    with pytest.raises(GitHubTransientError) as caught:
        await ping(http, auth)

    assert caught.value.status_code == 401
    assert len(token_requests(github)) == 2  # the first, and the one after the 401
    assert len(pings(github)) == 2


async def test_a_late_401_for_an_old_token_does_not_drop_the_new_one(
    auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    """Two requests with the same stale token both get 401: only one new token is needed."""
    first = auth.installation_credential(INSTALLATION_ID, REPO_ID)
    second = auth.installation_credential(INSTALLATION_ID, REPO_ID)
    stale = await first.authorization()

    await first.refresh(stale)
    fresh = await first.authorization()
    await second.refresh(stale)  # the second request's 401 arrives late
    assert await second.authorization() == fresh

    assert len(token_requests(github)) == 2


# --- the token request is refused ---------------------------------------------------------------


def suspend(github: FakeGitHub) -> None:
    github.installations[INSTALLATION_ID].suspended = True


def uninstall(github: FakeGitHub) -> None:
    del github.installations[INSTALLATION_ID]


def deny_pull_requests(github: FakeGitHub) -> None:
    del github.installations[INSTALLATION_ID].permissions["pull_requests"]


def change_nothing(github: FakeGitHub) -> None:
    return None


UNAVAILABLE = "a requested repository or permission is not available to the installation"


@pytest.mark.parametrize(
    ("setup", "repo_id", "status", "reason"),
    [
        (suspend, REPO_ID, 403, "the installation is suspended or the app was denied access"),
        (uninstall, REPO_ID, 404, "the installation no longer exists"),
        (deny_pull_requests, REPO_ID, 422, UNAVAILABLE),
        (change_nothing, 999, 422, UNAVAILABLE),  # 999 is not a repository of the installation
    ],
    ids=["suspended", "uninstalled", "permission-not-granted", "repo-not-in-installation"],
)
async def test_a_refused_token_request_is_permanent_and_not_cached(
    http: GitHubHttp,
    auth: GitHubAppAuth,
    github: FakeGitHub,
    setup: Callable[[FakeGitHub], None],
    repo_id: int,
    status: int,
    reason: str,
) -> None:
    setup(github)

    with pytest.raises(GitHubPermanentError) as caught:
        await ping(http, auth, repo_id)

    assert isinstance(caught.value, PermanentError)
    assert caught.value.status_code == status
    assert str(caught.value) == (
        f"installation token refused, {reason}: POST {TOKEN_PATH} answered {status}"
    )
    assert github.issued == []
    assert pings(github) == []  # nothing else was attempted without a token


async def test_a_token_request_that_failed_is_tried_again_by_the_next_call(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    suspend(github)
    with pytest.raises(GitHubPermanentError):
        await ping(http, auth)

    github.installations[INSTALLATION_ID].suspended = False
    await ping(http, auth)

    assert len(token_requests(github)) == 2
    assert len(github.issued) == 1


async def test_a_server_error_at_the_token_request_is_transient(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.fail("POST", TOKEN_PATH, 502)

    with pytest.raises(GitHubTransientError) as caught:
        await ping(http, auth)

    assert str(caught.value) == f"POST {TOKEN_PATH} answered 502"


async def test_a_rate_limited_token_request_says_how_long_to_wait(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.fail(
        "POST", TOKEN_PATH, 403, headers={"retry-after": "42"}, message="secondary rate limit"
    )

    with pytest.raises(GitHubTransientError) as caught:
        await ping(http, auth)

    assert caught.value.retry_after == timedelta(seconds=42)


@pytest.mark.parametrize(
    "body",
    [
        {"expires_at": "2026-01-01T01:00:00Z"},
        {"token": "tok-in-a-bad-body-31ab", "expires_at": "not-a-date"},
        {"token": "tok-in-a-bad-body-31ab", "expires_at": "2026-01-01T01:00:00"},  # no time zone
        {"token": "tok-in-a-bad-body-31ab"},
    ],
    ids=["no-token", "bad-date", "naive-date", "no-expiry"],
)
async def test_a_malformed_token_response_is_transient_and_never_quoted(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub, body: dict[str, str]
) -> None:
    github.add_route("POST", TOKEN_PATH, lambda request: json_response(201, body), auth="app")

    with pytest.raises(GitHubTransientError) as caught:
        await ping(http, auth)

    assert str(caught.value) == f"POST {TOKEN_PATH} returned an unexpected body"
    assert "31ab" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert (
        caught.value.__suppress_context__
    )  # raised ``from None``: the validation error quotes the token


# --- the app's identity -------------------------------------------------------------------------


async def test_the_identity_is_the_slug_and_id_of_the_app(
    auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    identity = await auth.app_identity()

    assert identity == AppIdentity(slug=APP_SLUG, id=APP_ID)
    [request] = github.requests_to("GET", "/app")
    assert (request.header("authorization") or "").startswith("Bearer ")


async def test_the_identity_is_fetched_once(auth: GitHubAppAuth, github: FakeGitHub) -> None:
    github.delay("GET", "/app", 0.05)

    results = await asyncio.gather(*(auth.app_identity() for _ in range(5)))
    await auth.app_identity()

    assert len(set(results)) == 1
    assert len(github.requests_to("GET", "/app")) == 1


async def test_a_failed_identity_request_is_not_cached(
    auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    github.fail("GET", "/app", 500)
    with pytest.raises(GitHubTransientError):
        await auth.app_identity()

    assert (await auth.app_identity()).slug == APP_SLUG


# --- no secret in a message, a log or a repr ----------------------------------------------------


async def test_no_key_jwt_or_token_reaches_a_message_a_log_or_a_repr(
    http: GitHubHttp,
    auth: GitHubAppAuth,
    github: FakeGitHub,
    clock: FakeClock,
    app_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []

    async def attempt() -> None:
        try:
            await ping(http, auth)
        except (GitHubPermanentError, GitHubTransientError) as exc:
            texts.extend([str(exc), repr(exc)])

    await ping(http, auth)  # a good run first, so a token is in the cache
    github.revoke_all()
    await ping(http, auth)  # then a 401 and a renewal
    for status in (403, 404, 422, 500):
        github.fail("POST", TOKEN_PATH, status)
        clock.advance(timedelta(hours=2))  # the cached token is gone, so the request is made
        await attempt()
    suspend(github)
    clock.advance(timedelta(hours=2))
    await attempt()
    await auth.app_identity()
    texts.extend([caplog.text, repr(auth), repr(vars(auth))])

    jwts = {
        (request.header("authorization") or "").removeprefix("Bearer ")
        for request in [*github.requests_to("GET", "/app"), *token_requests(github)]
    }
    body = "".join(pem_of(app_key).splitlines()[1:-1])
    secrets = {t.token for t in github.issued} | jwts | {pem_of(app_key), body[:60], CANARY}
    assert len(secrets) >= 5  # tokens, JWTs, the key and the canary were all really in play
    for secret in secrets:
        assert not any(secret in text for text in texts), f"a {len(secret)}-character secret leaked"
