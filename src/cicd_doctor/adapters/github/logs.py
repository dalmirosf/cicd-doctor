"""The GitHub ``LogSource``: the jobs of a run attempt and the tail of a job log (D-71).

``GET /repos/{o}/{r}/actions/jobs/{id}/logs`` answers 302 to a signed blob URL. The URL is fetched
with a client of its own that has no credentials and no GitHub headers (SPEC §9.1): the signature
in its query is the only secret it carries, and it never reaches a log line or an error message.

- Over ``max_bytes`` (``Content-Length``): the stream is closed and the tail is asked with
  ``Range: bytes={length - max_bytes}-``. Only offsets are used: the blob ignores a suffix range.
- Otherwise (small log, no ``Content-Length``, a server that ignores the ``Range``, a gzip blob)
  the chunks go through a ``TailBuffer``, which keeps the last ``max_bytes`` and stops reading at
  100 MB.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2

from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import Credential, GitHubHttp, GitHubPermanentError
from cicd_doctor.adapters.github.jobs import InvalidJobsPayloadError, parse_jobs
from cicd_doctor.core.errors import LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.logs.tail import HARD_READ_LIMIT, TailBuffer
from cicd_doctor.core.models import JobRef, RawLog, RunRef, WorkflowJob

logger = logging.getLogger(__name__)

JOBS_TEMPLATE = "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
LOG_TEMPLATE = "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"

BLOB_TIMEOUT = httpx2.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
# Where GitHub keeps logs (the spike saw Azure blobs). Another host is still fetched, without
# credentials, but it is worth a warning.
EXPECTED_BLOB_HOST_SUFFIXES = (".blob.core.windows.net", ".githubusercontent.com", ".github.com")


class GitHubLogSource:
    def __init__(
        self,
        http: GitHubHttp,
        auth: GitHubAppAuth,
        *,
        blob_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        # No ``base_url``, no default headers, no credential: nothing of GitHub's goes to the blob.
        self._blobs = httpx2.AsyncClient(
            timeout=BLOB_TIMEOUT, follow_redirects=False, transport=blob_transport
        )

    async def aclose(self) -> None:
        await self._blobs.aclose()

    async def jobs(self, run: RunRef) -> list[WorkflowJob]:
        """Every job of this attempt. Always the attempt endpoint: the one without an attempt
        changes after a later re-run."""
        owner, repo = _split(run.repo_full_name)
        page = await self._http.paginate(
            JOBS_TEMPLATE,
            path={"owner": owner, "repo": repo, "run_id": run.run_id, "attempt": run.run_attempt},
            credential=self._credential(run),
            items_key="jobs",
        )
        try:
            return parse_jobs({"jobs": page.items})
        except InvalidJobsPayloadError as exc:  # names fields, never values
            raise TransientError(f"GET {JOBS_TEMPLATE}: {exc}") from None

    async def job_log(self, job: JobRef, max_bytes: int) -> RawLog:
        url = await self._redirect(job)
        return await self._download(url, max_bytes)

    # --- the API endpoint ------------------------------------------------------------------

    async def _redirect(self, job: JobRef) -> httpx2.URL:
        owner, repo = _split(job.run.repo_full_name)
        try:
            response = await self._http.request(
                "GET",
                LOG_TEMPLATE,
                path={"owner": owner, "repo": repo, "job_id": job.job_id},
                credential=self._credential(job.run),
                allow_redirect=True,
            )
        except GitHubPermanentError as exc:
            if exc.status_code in (404, 410):
                raise LogsUnavailable(str(exc), status_code=exc.status_code) from exc
            raise
        location = response.headers.get("location")
        if response.status_code < 300 or not location:
            raise TransientError(f"GET {LOG_TEMPLATE}: no redirect to the log")
        try:
            url = httpx2.URL(location)
        except httpx2.InvalidURL:
            raise PermanentError(f"GET {LOG_TEMPLATE}: the redirect is not a valid URL") from None
        if url.scheme != "https" or not url.host:
            raise PermanentError(f"GET {LOG_TEMPLATE}: the redirect is not an https URL")
        if not url.host.endswith(EXPECTED_BLOB_HOST_SUFFIXES):
            logger.warning("the log of a job is served from an unexpected host: %s", url.host)
        return url

    def _credential(self, run: RunRef) -> Credential:
        return self._auth.installation_credential(run.installation_id, run.repo_id)

    # --- the blob --------------------------------------------------------------------------

    async def _download(self, url: httpx2.URL, max_bytes: int) -> RawLog:
        try:
            async with self._open(url, offset=None) as response:
                length = _content_length(response)
                if not _can_range(response, length, max_bytes):
                    return await _consume(response, max_bytes, ranged=False)
            assert length is not None
            # The first stream is closed. The second request uses the same signed URL right away;
            # a retry of the job asks for a new redirect instead of reusing it (D-71).
            async with self._open(url, offset=length - max_bytes) as response:
                return await _consume(response, max_bytes, ranged=response.status_code == 206)
        except httpx2.TimeoutException:
            raise TransientError("log blob: timed out") from None
        except httpx2.RequestError as exc:
            raise TransientError(f"log blob: connection error ({type(exc).__name__})") from None

    @asynccontextmanager
    async def _open(self, url: httpx2.URL, *, offset: int | None) -> AsyncIterator[httpx2.Response]:
        headers = {"Range": f"bytes={offset}-"} if offset is not None else {}
        async with self._blobs.stream("GET", url, headers=headers) as response:
            _raise_for_blob_status(response)
            yield response


def _raise_for_blob_status(response: httpx2.Response) -> None:
    status = response.status_code
    if status < 300:
        return
    if status in (404, 410):
        raise LogsUnavailable(f"log blob answered {status}", status_code=status)
    if status == 403:  # the signature expired: a retry asks for a new redirect
        raise TransientError("log blob answered 403 (the signed URL was rejected or expired)")
    if status >= 500 or status in (408, 429):
        raise TransientError(f"log blob answered {status}")
    raise PermanentError(f"log blob answered {status}")


def _content_length(response: httpx2.Response) -> int | None:
    value = response.headers.get("content-length", "")
    return int(value) if value.isascii() and value.isdigit() else None


def _can_range(response: httpx2.Response, length: int | None, max_bytes: int) -> bool:
    """A ``Range`` helps only on an identity-encoded body that is longer than ``max_bytes`` and
    comes from a server that accepts ranges. Offsets of a compressed body are not offsets of the
    log, so a gzip body is counted after decompression instead."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    return (
        length is not None
        and length > max_bytes
        and encoding in ("", "identity")
        and response.headers.get("accept-ranges", "bytes").strip().lower() != "none"
    )


async def _consume(response: httpx2.Response, max_bytes: int, *, ranged: bool) -> RawLog:
    buffer = TailBuffer(max_bytes, max(HARD_READ_LIMIT, max_bytes))
    async for chunk in response.aiter_bytes():
        if not buffer.feed(chunk):
            break  # the hard limit: leaving the ``async with`` closes the stream
    log = buffer.result()
    if not ranged:
        return log
    # A 206 starts at an arbitrary byte: drop the partial first line. (A cut that lands exactly on
    # a line start loses that one line.) A tail without any newline is kept as it is.
    _, newline, rest = log.text.partition("\n")
    return RawLog(text=rest if newline else log.text, truncated=True)


def _split(full_name: str) -> tuple[str, str]:
    owner, _, repo = full_name.partition("/")
    return owner, repo
