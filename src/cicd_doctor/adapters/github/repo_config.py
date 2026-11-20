"""``.github/cicd-doctor.yml`` of a repository, read from its default branch only (D-90).

The request never carries a ``ref``: GitHub then answers with the file of the repository's default
branch, so a pull request (from a fork or not) cannot choose its own configuration. This class has
no way to ask for another ref, which is the guarantee, and a test asserts that no request has one.

Outcomes: 404 is "no file" (the defaults); a directory, a symlink, an unknown encoding, a file over
the cap or a body that is not what the API documents is an ``invalid`` file; a 403 or 422 from the
token or the endpoint is ``unreadable`` (the installation has not accepted ``contents: read``;
the defaults apply, with a WARNING); a 5xx, a rate limit or a timeout is a ``TransientError`` that
the job retries, never the defaults. A response body is never put into a message or a log.
"""

import base64
import binascii
import logging
from typing import Any

from cicd_doctor.adapters.github.auth import (
    CONFIG_PERMISSIONS,
    GitHubAppAuth,
    InstallationTokenRefused,
)
from cicd_doctor.adapters.github.http import GitHubHttp, GitHubPermanentError
from cicd_doctor.core.models import LoadedConfig, RepoRef
from cicd_doctor.core.repo_config import (
    DEFAULT_LOADED,
    MAX_CONFIG_BYTES,
    invalid,
    parse_repo_config,
)

logger = logging.getLogger(__name__)

# A literal path: the file name is fixed, so there is nothing to encode into the template.
CONFIG_FILE = "/repos/{owner}/{repo}/contents/.github/cicd-doctor.yml"
UNREADABLE = frozenset({403, 422})


class GitHubRepoConfigSource:
    def __init__(self, http: GitHubHttp, auth: GitHubAppAuth) -> None:
        self._http = http
        self._auth = auth

    async def load(self, repo: RepoRef) -> LoadedConfig:
        owner, _, name = repo.full_name.partition("/")
        try:
            response = await self._http.request(
                "GET",
                CONFIG_FILE,
                path={"owner": owner, "repo": name},
                credential=self._auth.installation_credential(
                    repo.installation_id, repo.repo_id, CONFIG_PERMISSIONS
                ),
            )
        except GitHubPermanentError as exc:
            status = exc.status_code
            if status == 404 and not isinstance(exc, InstallationTokenRefused):
                return DEFAULT_LOADED
            if status in UNREADABLE:
                logger.warning(
                    "repo_id=%d: the configuration cannot be read (%s), using the defaults",
                    repo.repo_id,
                    status,
                )
                return LoadedConfig(state="unreadable")
            raise
        return _parse_response(_json(response))


def _json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _parse_response(body: Any) -> LoadedConfig:
    """The ``contents`` answer of one file as a ``LoadedConfig``."""
    if not isinstance(body, dict):
        return invalid("response")  # a directory is a list
    if body.get("type") != "file":
        return invalid("type")
    size = body.get("size")
    if not isinstance(size, int) or size > MAX_CONFIG_BYTES:
        return invalid("size")
    content = body.get("content")
    if body.get("encoding") != "base64" or not isinstance(content, str):
        return invalid("encoding")
    try:
        raw = base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError):
        return invalid("encoding")
    if len(raw) > MAX_CONFIG_BYTES:
        return invalid("size")
    try:
        text = raw.decode("utf-8-sig")  # a BOM is allowed; any other byte sequence is not
    except UnicodeDecodeError:
        return invalid("encoding")
    return parse_repo_config(text)
