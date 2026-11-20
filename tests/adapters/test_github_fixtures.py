"""The adapters against responses recorded from the real GitHub API (``tests/fixtures/github``).

The fixtures were recorded read-only from the dev app on 2026-11-16 and sanitized (see the README
there). Each test serves one of them through ``FakeGitHub`` and checks that the adapter still
reads it: if a fixture stops parsing, or an adapter stops understanding the real shape, it fails.
Fixtures are only loaded and selected here, never edited on disk.
"""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import (
    GitHubHttp,
    GitHubPermanentError,
    GitHubTransientError,
)
from cicd_doctor.adapters.github.jobs import parse_jobs
from cicd_doctor.adapters.github.logs import GitHubLogSource
from cicd_doctor.adapters.github.pulls import PULLS, GitHubPullRequests
from cicd_doctor.adapters.github.reporter import (
    CHECK_RUNS,
    COMMIT_CHECK_RUNS,
    GitHubReporter,
    external_id_for,
)
from cicd_doctor.adapters.github.webhooks import route_event
from cicd_doctor.core.errors import LogsUnavailable
from cicd_doctor.core.logs.parse import parse_log
from cicd_doctor.core.models import JobKind, JobRef, RecordOutcomePayload
from tests.factories import T0, make_run_ref
from tests.github_fake import (
    APP_ID,
    BLOB_TEMPLATE,
    INSTALLATION_ID,
    JOBS_TEMPLATE,
    LOG_TEMPLATE,
    REPO_ID,
    FakeGitHub,
    FakeRepoWrites,
    GitHubLogsFake,
    json_response,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
SHA = "a" * 40


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def serve(github: FakeGitHub, method: str, template: str, body: Any, *, auth: Any = None) -> None:
    """Answer ``template`` with a recorded body (200)."""
    handler = lambda request: json_response(200, body)  # noqa: E731
    if auth is None:
        github.add_route(method, template, handler)
    else:
        github.add_route(method, template, handler, auth=auth)


def real_run(**changes: object) -> Any:
    fields: dict[str, object] = {
        "installation_id": INSTALLATION_ID,
        "repo_id": REPO_ID,
        "repo_full_name": "acme/cicd-doctor-lab",
        "head_sha": SHA,
    }
    return make_run_ref(**(fields | changes))


# --- every fixture ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_every_fixture_is_json_without_credentials(path: Path) -> None:
    text = path.read_text("utf-8")

    assert json.loads(text) is not None
    assert not re.search(r"-----BEGIN|ghs_[A-Za-z0-9]{20}|eyJ[A-Za-z0-9_-]{20}", text)
    assert "sig=" not in text  # the signed query of a log URL is never recorded


def test_there_are_twenty_four_recorded_fixtures() -> None:
    assert len(list(FIXTURES.glob("*.json"))) == 24


# --- jobs ------------------------------------------------------------------------------------


def test_the_jobs_of_a_real_attempt_parse() -> None:
    payload = fixture("jobs_run_attempt")

    jobs = parse_jobs(payload)

    assert len(jobs) == payload["total_count"] >= 1
    first = jobs[0]
    assert first.job_id == payload["jobs"][0]["id"]
    assert first.runner_os == "Linux"
    assert first.steps
    assert [step.number for step in first.steps] == sorted(step.number for step in first.steps)


def test_a_real_timed_out_job_ends_cancelled_with_its_step_cancelled() -> None:
    (job,) = parse_jobs(fixture("jobs_job_timeout"))

    assert job.conclusion == "cancelled"  # U7: a job timeout ends cancelled, not timed_out
    assert any(step.conclusion == "cancelled" for step in job.steps)


async def test_the_log_source_lists_the_jobs_of_a_real_attempt(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    payload = fixture("jobs_run_attempt")
    GitHubLogsFake(github)
    serve(github, "GET", JOBS_TEMPLATE, payload)
    source = GitHubLogSource(http, auth, blob_transport=github.transport())

    jobs = await source.jobs(real_run(run_attempt=payload["jobs"][0]["run_attempt"]))
    await source.aclose()

    assert [job.job_id for job in jobs] == [job["id"] for job in payload["jobs"]]


def test_the_timeout_annotations_carry_the_text_the_timeout_rule_waits_for() -> None:
    messages = [annotation["message"] for annotation in fixture("annotations_job_timeout")]

    assert any("exceeded the maximum execution time" in message for message in messages)


# --- app authentication ----------------------------------------------------------------------


async def test_the_real_app_object_gives_the_identity(
    auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    app = fixture("app")
    serve(github, "GET", "/app", app, auth="app")

    identity = await auth.app_identity()

    assert (identity.slug, identity.id) == (app["slug"], app["id"])
    assert app["permissions"]["checks"] == "write"


async def test_the_real_installation_token_response_is_read(
    auth: GitHubAppAuth, github: FakeGitHub, clock: FakeClock
) -> None:
    recorded = fixture("installation_token")
    # GitHub's token is not recorded (sanitized): a placeholder takes its place.
    body = {**recorded, "token": "placeholder-token", "expires_at": "2099-01-01T00:00:00Z"}
    serve(github, "POST", "/app/installations/{installation_id}/access_tokens", body, auth="app")

    header = await auth.installation_credential(INSTALLATION_ID, REPO_ID).authorization()

    assert header == "Bearer placeholder-token"
    assert recorded["repository_selection"] == "selected"
    assert recorded["permissions"]["checks"] == "write"
    assert {"expires_at", "permissions", "repositories"} <= recorded.keys()


# --- check runs and pull requests ------------------------------------------------------------


def test_the_listings_of_real_check_runs_have_the_fields_the_reporter_reads() -> None:
    for name in ("check_runs_lab", "check_runs_me"):
        listing = fixture(name)
        assert listing["check_runs"]
        for item in listing["check_runs"]:
            assert isinstance(item["id"], int)
            assert isinstance(item["external_id"], str)
            assert isinstance(item["app"]["id"], int)
            assert item["app"]["slug"] == "github-actions"


async def test_a_real_listing_without_our_check_makes_the_reporter_create_it(
    http: GitHubHttp, auth: GitHubAppAuth, clock: FakeClock, github: FakeGitHub
) -> None:
    repo = FakeRepoWrites(github)
    serve(github, "GET", COMMIT_CHECK_RUNS, fixture("check_runs_lab"))  # Actions' check runs only
    reporter = GitHubReporter(http, auth, clock)

    check_id = await reporter.start_check(real_run(workflow_name="pr-py-modnf"), None)

    assert repo.check_runs[check_id]["name"] == "CICD Doctor: pr-py-modnf"
    assert len(github.requests_to("POST", CHECK_RUNS)) == 1


async def test_our_check_in_the_real_listing_shape_is_found_by_external_id(
    http: GitHubHttp, auth: GitHubAppAuth, clock: FakeClock, github: FakeGitHub
) -> None:
    repo = FakeRepoWrites(github)
    run = real_run(workflow_name="pr-py-modnf")
    listing = fixture("check_runs_lab")
    # One real check run (full shape) made ours: its app is this one and its external_id ours.
    ours = repo.add_check_run(
        name="CICD Doctor: pr-py-modnf",
        head_sha=SHA,
        external_id=external_id_for(run.run_id),
        status="completed",
        conclusion="neutral",
    )
    ours_in_real_shape = {
        **listing["check_runs"][0],
        "id": ours["id"],
        "name": ours["name"],
        "external_id": ours["external_id"],
        "app": {**listing["check_runs"][0]["app"], "id": APP_ID, "slug": "cicd-doctor-test"},
    }
    serve(
        github,
        "GET",
        COMMIT_CHECK_RUNS,
        {**listing, "check_runs": [*listing["check_runs"], ours_in_real_shape]},
    )
    reporter = GitHubReporter(http, auth, clock)

    check_id = await reporter.start_check(run, None)

    assert check_id == ours["id"]
    assert github.requests_to("POST", CHECK_RUNS) == []


async def test_a_real_pull_listing_resolves_the_pull_request_of_a_run(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    listing = fixture("pulls_list")
    pull = listing[0]
    serve(github, "GET", PULLS, listing)
    run = real_run(
        repo_full_name=pull["base"]["repo"]["full_name"],
        head_sha=pull["head"]["sha"],
        head_branch=pull["head"]["ref"],
        head_repo_full_name=pull["head"]["repo"]["full_name"],
        event="pull_request",
        is_fork=False,
        pr_number=None,
    )

    found = await GitHubPullRequests(http, auth).find(run)

    assert found == pull["number"]
    (request,) = github.requests_to("GET", PULLS)
    assert request.query["head"] == pull["head"]["label"]


# --- logs ------------------------------------------------------------------------------------


def _blob_log(size: int) -> bytes:
    """A log of exactly ``size`` bytes that starts with the BOM the real blob has (U2)."""
    line = b"2026-11-14T01:00:00.0000000Z a log line of the real blob\n"
    body = b"\xef\xbb\xbf"
    while len(body) < size:
        body += line
    return body[:size]


async def test_the_real_redirect_and_blob_headers_drive_the_log_source(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    redirect = fixture("log_redirect")
    headers = fixture("log_blob_headers")
    size = int(headers["default_encoding"]["headers"]["content-length"])
    assert redirect["status"] == 302
    assert redirect["location"]["scheme"] == "https"
    logs = GitHubLogsFake(github)
    logs.redirect_host = redirect["location"]["host"]  # the host GitHub really answered with
    body = _blob_log(size)
    logs.set_log(
        7, body, headers={"Last-Modified": headers["default_encoding"]["headers"]["last-modified"]}
    )
    source = GitHubLogSource(http, auth, blob_transport=github.transport())
    run = real_run()
    job = JobRef(run=run, job_id=7, job_name="run")
    tail = int(headers["range_offset_identity"]["headers"]["content-length"])

    raw = await source.job_log(job, tail)
    await source.aclose()

    expected_offset = int(
        headers["range_offset_identity"]["headers"]["content-range"].split()[1].split("-")[0]
    )
    (first, second) = github.requests_to("GET", BLOB_TEMPLATE)
    assert first.header("range") is None
    assert second.header("range") == f"bytes={expected_offset}-"  # only offsets, never a suffix
    assert raw.truncated is True
    assert 0 < len(raw.text.encode()) <= tail  # the tail may drop a cut first line
    assert size - expected_offset == tail
    assert redirect["location"]["host"] in {r.host for r in github.requests}
    # the BOM of the real blob never reaches a line
    first_line = parse_log(_blob_log(200).decode("utf-8", "replace"))[0]
    assert first_line.second is not None  # the timestamp was read, so no BOM stood before it
    assert not first_line.content.startswith("﻿")


def test_the_recorded_blob_has_no_content_encoding_and_ignores_a_suffix_range() -> None:
    headers = fixture("log_blob_headers")

    for variant in ("default_encoding", "identity"):
        assert "content-encoding" not in headers[variant]["headers"]  # U2
        assert headers[variant]["first_two_bytes_hex"] == "efbb"
    assert headers["range_offset_identity"]["status"] == 206  # U3
    assert headers["range_suffix_identity"]["status"] == 200  # the blob ignores a suffix


async def test_the_real_404_of_a_missing_log_means_the_log_is_unavailable(
    http: GitHubHttp, auth: GitHubAppAuth, github: FakeGitHub
) -> None:
    recorded = fixture("log_not_found")
    github.add_route(
        "GET", LOG_TEMPLATE, lambda request: json_response(recorded["status"], recorded["body"])
    )
    source = GitHubLogSource(http, auth, blob_transport=github.transport())
    run = real_run()

    with pytest.raises(LogsUnavailable) as caught:
        await source.job_log(JobRef(run=run, job_id=9, job_name="run"), 1000)
    await source.aclose()

    assert caught.value.status_code == 404
    assert recorded["body"]["message"] not in str(caught.value)  # the body is never copied


# --- error mapping ---------------------------------------------------------------------------


async def test_the_real_403_of_a_missing_permission_is_permanent_and_names_the_permission(
    http: GitHubHttp, github: FakeGitHub
) -> None:
    recorded = fixture("forbidden_secrets")
    # The spike measured this header on the same answer; the fixture keeps the body only.
    headers = {"x-accepted-github-permissions": "secrets=read", "x-ratelimit-remaining": "4990"}
    github.add_route("GET", "/things", lambda request: json_response(200, {}), auth="none")
    github.fail(
        "GET", "/things", recorded["status"], headers=headers, message=recorded["body"]["message"]
    )

    with pytest.raises(GitHubPermanentError) as caught:
        await http.request("GET", "/things")

    assert caught.value.status_code == 403
    assert "secrets=read" in str(caught.value)
    assert recorded["body"]["message"] not in str(caught.value)


async def test_the_real_rate_limit_counters_make_a_transient_error_with_the_wait(
    http: GitHubHttp, github: FakeGitHub, clock: FakeClock
) -> None:
    core = fixture("rate_limit")["resources"]["core"]
    assert core["limit"] == 5000
    reset = int(clock.now().timestamp()) + 90  # the recorded reset is of another day
    headers = {
        "x-ratelimit-limit": str(core["limit"]),
        "x-ratelimit-remaining": "0",
        "x-ratelimit-used": str(core["limit"]),
        "x-ratelimit-reset": str(reset),
        "x-ratelimit-resource": "core",
    }
    github.add_route("GET", "/things", lambda request: json_response(200, {}), auth="none")
    github.fail("GET", "/things", 403, headers=headers, message="API rate limit exceeded")

    with pytest.raises(GitHubTransientError) as caught:
        await http.request("GET", "/things")

    assert caught.value.retry_after == timedelta(seconds=90)


def test_the_log_redirect_carries_the_rate_limit_headers_the_client_reads() -> None:
    headers = fixture("log_redirect")["headers"]

    assert {"x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"} <= headers.keys()


# --- the rest of the recorded objects --------------------------------------------------------


def test_the_remaining_recorded_objects_have_the_shape_the_runbook_relies_on() -> None:
    installations = fixture("installations")
    repositories = fixture("installation_repositories")
    run = fixture("workflow_run_dispatch")
    runs = fixture("workflow_runs_list")

    assert installations
    assert {"cicd-doctor", "cicd-doctor-lab"} <= {r["name"] for r in repositories["repositories"]}
    assert run["event"] == "workflow_dispatch"
    assert run["pull_requests"] == []  # R1: empty for a workflow_dispatch
    assert runs["workflow_runs"]
    assert fixture("hook_config")["content_type"] == "json"
    assert fixture("hook_deliveries")


@pytest.mark.parametrize(
    ("name", "event", "branch"),
    [
        ("workflow_run_success_pull_request", "pull_request", "docs/branch-workflow"),
        ("workflow_run_success_push", "push", "main"),
    ],
)
def test_a_recorded_successful_run_becomes_a_record_outcome_job(
    name: str, event: str, branch: str
) -> None:
    run = fixture(name)
    # The webhook envelope is not part of the recording: build it around the run, as GitHub does
    # (the ``repository`` of the envelope is the one of the run, here with a default branch).
    payload = {
        "action": "completed",
        "workflow_run": run,
        "repository": {**run["repository"], "default_branch": "main"},
        "installation": {"id": 90000001},
    }

    job = route_event("workflow_run", payload, received_at=T0, max_attempts=5)

    assert job is not None
    assert job.kind is JobKind.RECORD_OUTCOME
    parsed = RecordOutcomePayload.model_validate(job.payload)
    assert (parsed.run.event, parsed.run.head_branch) == (event, branch)
    assert re.fullmatch(r"[0-9a-f]{40}", parsed.run.head_sha)
    assert job.dedupe_key == f"outcome:{run['id']}:{run['run_attempt']}"
