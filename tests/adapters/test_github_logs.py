"""The GitHub log source (D-71): jobs of an attempt and the tail of a job log.

Everything runs against ``FakeGitHub`` (the API host and the blob host over one
``httpx2.MockTransport``): no network. The fake puts ``CANARY`` in every error body and signs each
redirect with a new query, so the tests also show that neither a body nor a signed URL reaches an
exception message or a log line.
"""

import gzip
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest

from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.adapters.github.logs import GitHubLogSource
from cicd_doctor.app.container import HTTP_LOGGERS, configure_logging
from cicd_doctor.core.errors import LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.logs.parse import parse_log
from cicd_doctor.core.logs.select import is_job_timeout, select_failed_jobs
from cicd_doctor.core.logs.tail import HARD_READ_LIMIT
from cicd_doctor.core.models import JobRef
from tests.factories import make_run_ref
from tests.github_fake import (
    ANNOTATIONS_TEMPLATE,
    API_HOST,
    BLOB_HOST,
    BLOB_TEMPLATE,
    CANARY,
    INSTALLATION_ID,
    JOBS_TEMPLATE,
    LOG_TEMPLATE,
    REPO_ID,
    FakeGitHub,
    GitHubLogsFake,
    RecordedRequest,
    jobs_json,
)

RUN = make_run_ref(
    installation_id=INSTALLATION_ID, repo_id=REPO_ID, repo_full_name="acme/widgets", run_attempt=1
)
JOB = JobRef(run=RUN, job_id=77, job_name="build")
SIG = "fake-signature-"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
FIRST_LINE = "2026-11-12T12:00:00."


def lines(count: int, start: int = 0) -> bytes:
    return b"".join(
        f"2026-11-12T12:00:00.{n:07d}Z line {n}\n".encode() for n in range(start, start + count)
    )


@pytest.fixture(autouse=True)
def _http_loggers_as_the_app_sets_them() -> Iterator[None]:
    configure_logging()
    yield
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture
def fake(github: FakeGitHub) -> GitHubLogsFake:
    return GitHubLogsFake(github)


@pytest.fixture
async def source(
    github: FakeGitHub, http: GitHubHttp, auth: GitHubAppAuth, fake: GitHubLogsFake
) -> AsyncIterator[GitHubLogSource]:
    log_source = GitHubLogSource(http, auth, blob_transport=github.transport())
    yield log_source
    await log_source.aclose()


def blob_requests(github: FakeGitHub) -> list[RecordedRequest]:
    return github.requests_to("GET", BLOB_TEMPLATE)


# --- annotations (D-73) ----------------------------------------------------------------------


def note(message: str) -> dict[str, str]:
    return {"annotation_level": "failure", "title": "", "path": ".github", "message": message}


async def test_the_annotations_of_a_real_job_timeout_are_read_by_the_check_run_id(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    real = json.loads((FIXTURES / "annotations_job_timeout.json").read_text("utf-8"))
    fake.set_annotations(JOB.job_id, real)

    messages = await source.job_annotations(JOB)

    assert messages == [
        "The job has exceeded the maximum execution time of 1m0s",
        "The operation was canceled.",
    ]
    assert is_job_timeout(messages)
    [request] = github.requests_to("GET", ANNOTATIONS_TEMPLATE)
    assert request.params["job_id"] == str(JOB.job_id)
    assert request.has_authorization


async def test_a_manual_cancellation_has_no_timeout_annotation(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    fake.set_annotations(JOB.job_id, [note("The operation was canceled.")])

    assert not is_job_timeout(await source.job_annotations(JOB))


async def test_a_job_without_annotations_answers_an_empty_list(source: GitHubLogSource) -> None:
    assert await source.job_annotations(JOB) == []


async def test_annotations_follow_the_pages_and_keep_only_the_messages(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_annotations(JOB.job_id, [note(f"message {n}") for n in range(5)], page_size=2)

    messages = await source.job_annotations(JOB)

    assert messages == [f"message {n}" for n in range(5)]
    assert len(github.requests_to("GET", ANNOTATIONS_TEMPLATE)) == 3


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_5xx_while_reading_the_annotations_is_transient(
    source: GitHubLogSource, github: FakeGitHub, status: int
) -> None:
    github.fail("GET", ANNOTATIONS_TEMPLATE, status)

    with pytest.raises(TransientError) as excinfo:
        await source.job_annotations(JOB)

    assert CANARY not in str(excinfo.value)


async def test_a_rate_limit_while_reading_the_annotations_is_transient_with_retry_after(
    source: GitHubLogSource, github: FakeGitHub
) -> None:
    github.fail("GET", ANNOTATIONS_TEMPLATE, 429, headers={"retry-after": "42"})

    with pytest.raises(TransientError) as excinfo:
        await source.job_annotations(JOB)

    assert excinfo.value.retry_after == timedelta(seconds=42)


@pytest.mark.parametrize("status", [403, 404])
async def test_a_403_or_404_while_reading_the_annotations_is_permanent(
    source: GitHubLogSource, github: FakeGitHub, status: int
) -> None:
    github.fail("GET", ANNOTATIONS_TEMPLATE, status)

    with pytest.raises(PermanentError) as excinfo:
        await source.job_annotations(JOB)

    assert not isinstance(excinfo.value, LogsUnavailable)
    assert CANARY not in str(excinfo.value)


# --- jobs -----------------------------------------------------------------------------------


async def test_jobs_are_listed_from_the_attempt_endpoint(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_jobs(RUN.run_id, 1, jobs_json([1, 2], conclusion="failure"))

    jobs = await source.jobs(RUN)

    assert [job.job_id for job in jobs] == [1, 2]
    assert jobs[0].runner_os == "Linux"
    request = github.requests_to("GET", JOBS_TEMPLATE)[0]
    assert request.params["attempt"] == "1"
    assert request.has_authorization


async def test_jobs_follow_three_pages(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_jobs(RUN.run_id, 1, jobs_json(list(range(1, 8))), page_size=3)

    jobs = await source.jobs(RUN)

    assert [job.job_id for job in jobs] == list(range(1, 8))
    assert len(github.requests_to("GET", JOBS_TEMPLATE)) == 3


async def test_a_job_of_another_attempt_listed_in_this_one_is_taken_today(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    """U4, which the spike could not observe: a failed job with ``run_attempt = 1`` inside the
    listing of attempt 2. ``WorkflowJob`` has no ``run_attempt`` and nothing filters on it, so
    ``select_failed_jobs`` takes it (D-71 records this; no filter on purpose)."""
    attempt_2 = make_run_ref(run_attempt=2, installation_id=INSTALLATION_ID, repo_id=REPO_ID)
    fake.set_jobs(
        attempt_2.run_id,
        2,
        jobs_json([10], run_attempt=2, conclusion="success")
        + jobs_json([11], run_attempt=1, conclusion="failure"),
    )

    jobs = await source.jobs(attempt_2)
    failed = select_failed_jobs(attempt_2, jobs, 3)

    assert [item.job.job_id for item in failed] == [11]


async def test_a_malformed_jobs_listing_is_transient_and_names_no_values(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    fake.set_jobs(RUN.run_id, 1, [{"name": CANARY}])

    with pytest.raises(TransientError) as caught:
        await source.jobs(RUN)

    assert CANARY not in str(caught.value)


# --- the blob request -----------------------------------------------------------------------


async def test_the_blob_is_fetched_without_credentials_over_https(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(5))

    log = await source.job_log(JOB, 1000)

    assert log.text.splitlines()[0].endswith("line 0")
    assert not log.truncated
    api = github.requests_to("GET", LOG_TEMPLATE)
    assert len(api) == 1
    assert api[0].has_authorization
    assert api[0].host == API_HOST
    (blob,) = blob_requests(github)
    assert blob.host == BLOB_HOST
    assert not blob.has_authorization
    assert not any(name.startswith("x-github") for name in blob.headers)
    assert "github" not in (blob.header("accept") or "")
    assert "range" not in blob.headers


async def test_a_redirect_that_is_not_https_is_refused_and_never_fetched(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(5))
    fake.redirect_scheme = "http"

    with pytest.raises(PermanentError) as caught:
        await source.job_log(JOB, 1000)

    assert blob_requests(github) == []
    assert "sig" not in str(caught.value)


async def test_an_unexpected_host_is_fetched_without_credentials_and_logged_without_query(
    source: GitHubLogSource,
    fake: GitHubLogsFake,
    github: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    fake.redirect_host = "logs.example.net"
    caplog.set_level(logging.INFO)

    log = await source.job_log(JOB, 1000)

    assert log.text
    (blob,) = blob_requests(github)
    assert blob.host == "logs.example.net"
    assert not blob.has_authorization
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("logs.example.net" in r.getMessage() for r in warnings)
    assert SIG not in caplog.text
    assert "sig=" not in caplog.text


# --- the tail -------------------------------------------------------------------------------


async def test_a_long_log_is_fetched_with_an_offset_range_and_starts_at_a_complete_line(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    body = lines(2000)
    fake.set_log(JOB.job_id, body)
    max_bytes = 10_001  # not a multiple of a line: the cut falls inside a line

    log = await source.job_log(JOB, max_bytes)

    first, second = blob_requests(github)
    assert first.header("range") is None
    assert second.header("range") == f"bytes={len(body) - max_bytes}-"
    assert log.truncated
    assert log.text.endswith("line 1999\n")
    assert log.text.startswith(FIRST_LINE)  # a complete line
    assert len(log.text.encode()) <= max_bytes


async def test_a_range_that_lands_inside_one_giant_line_keeps_its_bytes(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    fake.set_log(JOB.job_id, b"x" * 5000)

    log = await source.job_log(JOB, 1000)

    assert log.text == "x" * 1000
    assert log.truncated


async def test_no_content_length_goes_through_the_ring_buffer(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(2000), content_length=False)

    log = await source.job_log(JOB, 10_001)

    assert len(blob_requests(github)) == 1
    assert log.truncated
    assert log.text.endswith("line 1999\n")
    assert log.text.startswith(FIRST_LINE)
    assert len(log.text.encode()) <= 10_001


async def test_a_server_that_ignores_the_range_still_keeps_the_tail(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(2000), ignore_range=True)

    log = await source.job_log(JOB, 10_001)

    assert len(blob_requests(github)) == 2  # asked for a range, got 200 and everything
    assert log.truncated
    assert log.text.endswith("line 1999\n")
    assert log.text.startswith(FIRST_LINE)
    assert len(log.text.encode()) <= 10_001


async def test_a_suffix_range_is_never_sent(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(2000))
    await source.job_log(JOB, 10_001)

    ranges = [r.header("range") or "" for r in blob_requests(github) if r.header("range")]
    assert ranges
    assert all(value.startswith("bytes=") and value.endswith("-") for value in ranges)
    assert not any(value.startswith("bytes=-") for value in ranges)


async def test_a_stream_of_100_mb_stops_and_does_not_read_more(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    chunk = lines(20_000)  # about 0.8 MB of complete lines

    async def endless() -> AsyncIterator[bytes]:
        while True:
            yield chunk

    state = fake.set_log(JOB.job_id, stream=endless)

    log = await source.job_log(JOB, 1_000_000)

    assert log.truncated
    assert HARD_READ_LIMIT <= state.served <= HARD_READ_LIMIT + 2 * len(chunk)
    assert len(log.text.encode()) <= 1_000_000


async def test_the_bom_and_carriage_returns_survive_to_parse_log(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    body = (
        b"\xef\xbb\xbf2026-11-12T12:00:00.0000001Z first\r\n"
        b"2026-11-12T12:00:01.0000001Z progress 10%\rprogress 100%\r\n"
    )
    fake.set_log(JOB.job_id, body)

    log = await source.job_log(JOB, 10_000)
    parsed = parse_log(log.text)

    assert log.text.startswith("﻿")  # plain utf-8, not utf-8-sig: parse_log strips it
    assert parsed[0].content.startswith("first")
    assert "\r" in parsed[1].content
    assert parsed[1].second == "2026-11-12T12:00:01"


async def test_a_gzip_blob_gets_no_range_and_counts_decompressed_bytes(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    packed = gzip.compress(lines(3000))
    max_bytes = 2000
    assert len(packed) > max_bytes  # the compressed length alone would ask for a Range
    fake.set_log(JOB.job_id, packed, headers={"Content-Encoding": "gzip"})

    log = await source.job_log(JOB, max_bytes)

    assert len(blob_requests(github)) == 1
    assert all(r.header("range") is None for r in blob_requests(github))
    assert log.truncated
    assert log.text.endswith("line 2999\n")
    assert len(log.text.encode()) <= max_bytes


# --- errors ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
async def test_logs_gone_at_the_api_endpoint_are_unavailable_with_their_status(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub, status: int
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", LOG_TEMPLATE, status)

    with pytest.raises(LogsUnavailable) as caught:
        await source.job_log(JOB, 1000)

    assert caught.value.status_code == status
    assert isinstance(caught.value, PermanentError)
    assert CANARY not in str(caught.value)


async def test_a_job_without_a_log_is_unavailable(source: GitHubLogSource) -> None:
    with pytest.raises(LogsUnavailable) as caught:
        await source.job_log(JOB, 1000)
    assert caught.value.status_code == 404


@pytest.mark.parametrize("status", [404, 410])
async def test_logs_gone_at_the_blob_are_unavailable(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub, status: int
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", BLOB_TEMPLATE, status)

    with pytest.raises(LogsUnavailable) as caught:
        await source.job_log(JOB, 1000)

    assert caught.value.status_code == status
    assert CANARY not in str(caught.value)
    assert SIG not in str(caught.value)


@pytest.mark.parametrize("template", [LOG_TEMPLATE, BLOB_TEMPLATE])
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_is_transient(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub, template: str, status: int
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", template, status)

    with pytest.raises(TransientError) as caught:
        await source.job_log(JOB, 1000)

    assert CANARY not in str(caught.value)
    assert SIG not in str(caught.value)


@pytest.mark.parametrize("template", [LOG_TEMPLATE, BLOB_TEMPLATE])
@pytest.mark.parametrize(
    "error",
    [httpx2.ReadTimeout("slow"), httpx2.ConnectError("refused"), httpx2.ReadError("reset")],
)
async def test_timeouts_and_connection_errors_are_transient(
    source: GitHubLogSource,
    fake: GitHubLogsFake,
    github: FakeGitHub,
    template: str,
    error: Exception,
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", template, error=error)

    with pytest.raises(TransientError) as caught:
        await source.job_log(JOB, 1000)

    assert SIG not in str(caught.value)


async def test_a_403_from_the_blob_is_transient_and_the_retry_gets_a_new_redirect(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", BLOB_TEMPLATE, 403)

    with pytest.raises(TransientError) as caught:
        await source.job_log(JOB, 1000)
    assert CANARY not in str(caught.value)
    assert SIG not in str(caught.value)

    log = await source.job_log(JOB, 1000)  # what the retried job does

    assert log.text
    queries = [r.raw_query for r in blob_requests(github)]
    assert len(queries) == 2
    assert queries[0] != queries[1]


async def test_a_403_from_the_api_endpoint_is_permanent(
    source: GitHubLogSource, fake: GitHubLogsFake, github: FakeGitHub
) -> None:
    fake.set_log(JOB.job_id, lines(3))
    github.fail("GET", LOG_TEMPLATE, 403, headers={"x-accepted-github-permissions": "actions=read"})

    with pytest.raises(PermanentError) as caught:
        await source.job_log(JOB, 1000)

    assert not isinstance(caught.value, LogsUnavailable)


async def test_a_failure_in_the_middle_of_the_stream_is_transient(
    source: GitHubLogSource, fake: GitHubLogsFake
) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield lines(10)
        raise httpx2.ReadError("connection reset")

    fake.set_log(JOB.job_id, stream=broken)

    with pytest.raises(TransientError):
        await source.job_log(JOB, 1000)


async def test_nothing_logs_the_signed_url_or_a_response_body(
    source: GitHubLogSource,
    fake: GitHubLogsFake,
    github: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.set_log(JOB.job_id, lines(2000))
    await source.job_log(JOB, 10_001)
    github.fail("GET", BLOB_TEMPLATE, 403)
    with pytest.raises(TransientError):
        await source.job_log(JOB, 10_001)

    assert SIG not in caplog.text
    assert "sig=" not in caplog.text
    assert "se=" not in caplog.text
    assert CANARY not in caplog.text
