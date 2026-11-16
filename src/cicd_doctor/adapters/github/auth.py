"""GitHub App authentication: the app JWT and installation tokens scoped to one repo (D-68).

Two credentials exist. The *app JWT* (RS256, signed with the private key) is only for the few
endpoints that act as the app (``GET /app``, the token request). An *installation token* is what
every other call uses: it is requested for one repository and for the permissions of one use case
only (SPEC §9.6, NFR-06), cached in memory until 5 minutes before it expires, and requested once
even when many tasks need it at the same time.

The private key lives in a ``SecretStr`` and, once loaded, in a key object whose ``repr`` shows
nothing. No error message, log line or ``repr`` here carries the key, a JWT or a token.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr, ValidationError

from cicd_doctor.adapters.github.http import (
    Credential,
    GitHubHttp,
    GitHubPermanentError,
    GitHubTransientError,
)
from cicd_doctor.core.ports import Clock

logger = logging.getLogger(__name__)

# The permission sets of the use cases. A token carries only what its use case needs, so a bug in
# one flow cannot use a permission that flow never asked for. P2 and P3 add ``contents: read``
# where a flow reads the repository.
Permissions = frozenset[tuple[str, str]]
DIAGNOSIS_PERMISSIONS: Permissions = frozenset(
    {("actions", "read"), ("checks", "write"), ("pull_requests", "write")}
)

JWT_ALGORITHM = "RS256"
JWT_BACKDATE = timedelta(seconds=60)  # ``iat`` in the past: absorbs a slow local clock
JWT_LIFETIME = timedelta(minutes=9)  # GitHub accepts up to 10
TOKEN_RENEW_MARGIN = timedelta(minutes=5)

_PEM_MARKER = "-----BEGIN"
_TOKEN_PATH = "/app/installations/{installation_id}/access_tokens"
# What a refused token request means, by status (the generic error says only the status).
_TOKEN_REFUSED = {
    403: "the installation is suspended or the app was denied access",
    404: "the installation no longer exists",
    422: "a requested repository or permission is not available to the installation",
}


class AppIdentity(BaseModel):
    """The app as ``GET /app`` describes it: its bot is ``<slug>[bot]``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    slug: str
    id: int


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")  # permissions, repositories and the rest are unused

    token: SecretStr
    expires_at: AwareDatetime


@dataclass(frozen=True)
class _TokenKey:
    installation_id: int
    repo_id: int
    permissions: Permissions


@dataclass(frozen=True)
class _CachedToken:
    token: SecretStr
    expires_at: datetime


def load_private_key(value: SecretStr, *, variable: str = "GITHUB_PRIVATE_KEY") -> RSAPrivateKey:
    """The app's RSA key from PEM content or from the path of a PEM file.

    PEM content may have real newlines or the two characters ``\\n`` (how a one-line ``.env``
    value is written). Failures raise ``ValueError`` naming the variable, never its value.
    """
    text = value.get_secret_value().strip()
    if _PEM_MARKER not in text:
        text = _read_key_file(text, variable)
    try:
        key = load_pem_private_key(text.replace("\\n", "\n").encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError(f"{variable} is not a valid unencrypted PEM private key") from None
    if not isinstance(key, RSAPrivateKey):
        raise ValueError(f"{variable} must be an RSA private key")
    return key


def _read_key_file(path: str, variable: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise ValueError(
            f"{variable} is neither PEM content nor the path of a readable file"
        ) from None


class _SingleFlight[K]:
    """One run of a coroutine per key, shared by everyone who asks while it is running.

    The run is a task that no single caller owns: if the caller that started it is cancelled, the
    others still get their result.
    """

    def __init__(self) -> None:
        self._flights: dict[K, asyncio.Future[Any]] = {}

    async def run[V](self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.ensure_future(factory())
            self._flights[key] = flight
            flight.add_done_callback(lambda done: self._landed(key, done))
        result: V = await asyncio.shield(flight)
        return result

    def _landed(self, key: K, flight: asyncio.Future[Any]) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
        if not flight.cancelled():
            flight.exception()  # mark it as retrieved: waiters that left never see it


class GitHubAppAuth:
    def __init__(
        self, http: GitHubHttp, *, app_id: int | str, private_key: RSAPrivateKey, clock: Clock
    ) -> None:
        self._http = http
        self._app_id = str(app_id)
        self._key = private_key
        self._clock = clock
        self._cache: dict[_TokenKey, _CachedToken] = {}
        self._tokens = _SingleFlight[_TokenKey]()
        self._identity: AppIdentity | None = None
        self._identity_flight = _SingleFlight[str]()
        self._app_credential = _AppCredential(self)

    def app_jwt(self) -> str:
        """A fresh app JWT. Signing takes about a millisecond, so none is cached."""
        now = self._clock.now()
        claims = {
            "iat": int((now - JWT_BACKDATE).timestamp()),
            "exp": int((now + JWT_LIFETIME).timestamp()),
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._key, algorithm=JWT_ALGORITHM)

    def installation_credential(
        self,
        installation_id: int,
        repo_id: int,
        permissions: Permissions = DIAGNOSIS_PERMISSIONS,
    ) -> Credential:
        """A credential for ``GitHubHttp`` that acts as the installation on one repository."""
        return _InstallationCredential(self, _TokenKey(installation_id, repo_id, permissions))

    async def app_identity(self) -> AppIdentity:
        """The slug and id of the app (``GET /app``). Fetched once per process."""
        if self._identity is None:
            self._identity = await self._identity_flight.run("app", self._fetch_identity)
        return self._identity

    # --- tokens ------------------------------------------------------------------------------

    async def _token(self, key: _TokenKey) -> SecretStr:
        cached = self._cache.get(key)
        if cached is not None and self._clock.now() < cached.expires_at - TOKEN_RENEW_MARGIN:
            return cached.token
        return await self._tokens.run(key, lambda: self._fetch_token(key))

    def _reject(self, key: _TokenKey, rejected: str) -> None:
        """Drop the cached token after a 401, unless a newer one already replaced it."""
        cached = self._cache.get(key)
        if cached is not None and f"Bearer {cached.token.get_secret_value()}" == rejected:
            del self._cache[key]

    async def _fetch_token(self, key: _TokenKey) -> SecretStr:
        body = {
            "repository_ids": [key.repo_id],
            "permissions": dict(sorted(key.permissions)),
        }
        try:
            response = await self._http.request(
                "POST",
                _TOKEN_PATH,
                path={"installation_id": key.installation_id},
                json=body,
                credential=self._app_credential,
            )
        except GitHubPermanentError as exc:
            reason = _TOKEN_REFUSED.get(exc.status_code or 0)
            if reason is None:
                raise
            raise GitHubPermanentError(
                f"installation token refused, {reason}: {exc}", status_code=exc.status_code
            ) from exc
        issued = _parse(_TokenResponse, response, f"POST {_TOKEN_PATH}")
        now = self._clock.now()
        # Tokens of a past hour would pile up otherwise: one entry per (installation, repo, set).
        self._cache = {k: v for k, v in self._cache.items() if v.expires_at > now}
        self._cache[key] = _CachedToken(issued.token, issued.expires_at)
        return issued.token

    async def _fetch_identity(self) -> AppIdentity:
        response = await self._http.request("GET", "/app", credential=self._app_credential)
        return _parse(AppIdentity, response, "GET /app")


class _AppCredential:
    """The app JWT: signed again on every use, so a 401 needs nothing refreshed."""

    def __init__(self, auth: GitHubAppAuth) -> None:
        self._auth = auth

    async def authorization(self) -> str:
        return f"Bearer {self._auth.app_jwt()}"

    async def refresh(self, rejected: str) -> None:
        return None


class _InstallationCredential:
    def __init__(self, auth: GitHubAppAuth, key: _TokenKey) -> None:
        self._auth = auth
        self._key = key

    async def authorization(self) -> str:
        return f"Bearer {(await self._auth._token(self._key)).get_secret_value()}"

    async def refresh(self, rejected: str) -> None:
        self._auth._reject(self._key, rejected)


def _parse[M: BaseModel](model: type[M], response: httpx2.Response, label: str) -> M:
    """Parse a body. The error says nothing of it: a validation error would quote the token."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        raise GitHubTransientError(
            f"{label} returned an unexpected body", status_code=response.status_code
        ) from None
