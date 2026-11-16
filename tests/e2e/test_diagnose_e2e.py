"""Offline end to end (P1c d2): signed webhook -> queue -> worker -> the REAL GitHub adapters.

``POST /webhooks/github`` goes through the real API with an in-memory queue; ``Worker.run_one``
leases the job and runs the real handler, use case and GitHub adapters, which talk to
``FakeGitHub`` (so the test sees the exact requests). The store, the ledger and the LLM are the
in-memory ones. No network and no Postgres.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import JobStatus, Publication, PublicationKind
from tests.diagnose_world import (
    PR_NUMBER,
    REPO_ID,
    DiagnoseWorld,
    diagnose_world,
    workflow_run_payload,
)
from tests.github_fake import DEFAULT_INSTALLATION_PERMISSIONS
from tests.webhooks import SECRET, encode, headers

WEBHOOK = "/webhooks/github"
CHECKS = "/repos/{owner}/{repo}/check-runs"
COMMIT_CHECK_RUNS = "/repos/{owner}/{repo}/commits/{ref}/check-runs"
CHECK = "/repos/{owner}/{repo}/check-runs/{check_run_id}"
COMMENTS = "/repos/{owner}/{repo}/issues/{issue_number}/comments"
COMMENT = "/repos/{owner}/{repo}/issues/comments/{comment_id}"
JOBS = "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
LOG = "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
APP = "/app"
TOKEN = "/app/installations/{installation_id}/access_tokens"
BLOB = "/actions-results/{blob_id}/workflow-job-run-{run_uuid}/logs/job/job-logs.txt"
MARKER = f"<!-- cicd-doctor:v1 pr={PR_NUMBER} workflow=7 -->"


class FlakyStore(InMemoryFailureStore):
    """Fails the next comment ``save_publication`` calls, as a crash right after the POST would."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.fail_comment_saves = 0

    async def save_publication(self, pub: Publication) -> bool:
        if self.fail_comment_saves and pub.key.kind is PublicationKind.COMMENT:
            self.fail_comment_saves -= 1
            raise TransientError("the database went away")
        return await super().save_publication(pub)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))


@pytest.fixture
async def world(clock: FakeClock) -> AsyncIterator[DiagnoseWorld]:
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=FlakyStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
    ) as built:
        yield built


def deliver(world: DiagnoseWorld, run_id: int = 5000, attempt: int = 1, *, delivery: str) -> int:
    """POST a signed ``workflow_run`` delivery to the real API; return its HTTP status."""
    deps = ApiDependencies(
        queue=world.queue,
        webhook_secrets=(SECRET,),
        ready=_ready,
        clock=world.clock,
    )
    body = encode(workflow_run_payload(run_id, attempt))
    with TestClient(create_app(deps)) as client:
        response = client.post(
            WEBHOOK, content=body, headers=headers("workflow_run", body, delivery=delivery)
        )
    return response.status_code


async def _ready() -> bool:
    return True


async def run_job(world: DiagnoseWorld) -> bool:
    return await world.worker.run_one("e2e")


# --- the whole flow --------------------------------------------------------------------------


async def test_a_signed_webhook_ends_in_one_check_run_and_one_comment(
    world: DiagnoseWorld,
) -> None:
    world.add_run()

    assert deliver(world, delivery="b1c2d3e4-0000-4000-8000-000000000001") == 202
    assert await run_job(world)

    assert world.calls() == [
        ("GET", APP),  # who am I (the bot login and id), once per process
        ("POST", TOKEN),  # one token for the repository and the diagnosis permissions
        ("GET", COMMIT_CHECK_RUNS),  # find the check run by its external_id (none yet)
        ("POST", CHECKS),  # in_progress
        ("GET", JOBS),
        ("GET", LOG),  # 302 to the blob host
        ("GET", BLOB),  # the signed blob, without Authorization
        ("PATCH", CHECK),  # completed, neutral
        ("GET", COMMENTS),  # find the sticky comment by its marker (none yet)
        ("POST", COMMENTS),
    ]
    [token] = world.requests("POST", TOKEN)
    assert token.json == {
        "repository_ids": [REPO_ID],
        "permissions": {"actions": "read", "checks": "write", "pull_requests": "write"},
    }
    assert token.has_authorization  # the app JWT
    assert set(token.json["permissions"].items()) <= set(DEFAULT_INSTALLATION_PERMISSIONS.items())

    [start] = world.requests("POST", CHECKS)
    assert start.json["status"] == "in_progress"
    assert start.json["name"] == "CICD Doctor: CI"
    assert start.json["external_id"] == "cicd-doctor:run:5000"
    [finish] = world.requests("PATCH", CHECK)
    assert (finish.json["status"], finish.json["conclusion"]) == ("completed", "neutral")
    assert finish.json["output"]["title"].startswith("Likely cause")

    [redirect] = world.requests("GET", LOG)
    [blob] = world.requests("GET", BLOB)
    assert redirect.has_authorization
    assert blob.host != redirect.host
    assert not blob.has_authorization  # the signed URL travels alone

    [comment] = world.requests("POST", COMMENTS)
    assert comment.json["body"].splitlines()[0] == MARKER
    assert len(world.writes.check_runs) == 1
    assert len(world.writes.comments) == 1

    job = await world.queue.get(1)
    assert job is not None
    assert (job.status, job.attempts) == (JobStatus.DONE, 1)
    assert len(world.llm.calls) == 1


async def test_a_duplicate_delivery_creates_no_second_job(world: DiagnoseWorld) -> None:
    world.add_run()
    delivery = "b1c2d3e4-0000-4000-8000-000000000002"

    assert deliver(world, delivery=delivery) == 202
    assert deliver(world, delivery=delivery) == 200  # the same delivery again
    assert deliver(world, delivery="b1c2d3e4-0000-4000-8000-000000000003") == 200  # same attempt

    assert await run_job(world)
    assert not await run_job(world)  # nothing else was queued
    assert len(world.requests("POST", CHECKS)) == 1


async def test_attempt_two_only_patches_the_check_run_and_the_comment(
    world: DiagnoseWorld,
) -> None:
    world.add_run(attempt=1)
    world.add_run(attempt=2)
    assert deliver(world, attempt=1, delivery="b1c2d3e4-0000-4000-8000-000000000004") == 202
    assert await run_job(world)
    first_calls = len(world.github.requests)

    assert deliver(world, attempt=2, delivery="b1c2d3e4-0000-4000-8000-000000000005") == 202
    assert await run_job(world)

    second = [(r.method, r.template) for r in world.github.requests[first_calls:]]
    assert ("POST", CHECKS) not in second
    assert ("POST", COMMENTS) not in second
    assert second.count(("PATCH", CHECK)) == 2  # back to in_progress, then completed
    assert second.count(("PATCH", COMMENT)) == 1
    assert len(world.writes.check_runs) == 1
    assert len(world.writes.comments) == 1
    [comment] = world.writes.comments.values()
    assert comment["body"].splitlines()[0] == MARKER
    for job_id in (1, 2):
        job = await world.queue.get(job_id)
        assert job is not None
        assert job.status is JobStatus.DONE


async def test_two_runs_of_the_same_pull_request_and_workflow_make_one_comment(
    world: DiagnoseWorld,
) -> None:
    world.add_run(5000)
    world.add_run(5001)
    world.github.delay("POST", COMMENTS, 0.05)  # make the two publications overlap
    assert deliver(world, 5000, delivery="b1c2d3e4-0000-4000-8000-000000000006") == 202
    assert deliver(world, 5001, delivery="b1c2d3e4-0000-4000-8000-000000000007") == 202

    first, second = await asyncio.gather(
        world.worker.run_one("e2e-a"), world.worker.run_one("e2e-b")
    )

    assert first
    assert second
    assert len(world.requests("POST", COMMENTS)) == 1
    assert len(world.writes.comments) == 1
    assert len(world.writes.check_runs) == 2  # one check run per run
    for job_id in (1, 2):
        job = await world.queue.get(job_id)
        assert job is not None
        assert job.status is JobStatus.DONE


async def test_a_comment_post_that_was_not_recorded_is_found_by_its_marker_on_retry(
    world: DiagnoseWorld, clock: FakeClock
) -> None:
    assert isinstance(world.store, FlakyStore)
    world.store.fail_comment_saves = 1  # GitHub got the comment, the database never heard of it
    world.add_run()
    assert deliver(world, delivery="b1c2d3e4-0000-4000-8000-000000000008") == 202

    assert await run_job(world)  # the use case fails: the job is retried later

    failed = await world.queue.get(1)
    assert failed is not None
    assert failed.status is JobStatus.QUEUED
    assert len(world.requests("POST", COMMENTS)) == 1
    downloads = len(world.requests("GET", BLOB))
    clock.advance(timedelta(minutes=10))

    assert await run_job(world)

    done = await world.queue.get(1)
    assert done is not None
    assert (done.status, done.attempts) == (JobStatus.DONE, 2)
    assert len(world.requests("POST", COMMENTS)) == 1  # still the one
    [patched] = world.requests("PATCH", COMMENT)
    assert patched.json["body"].splitlines()[0] == MARKER
    assert len(world.writes.comments) == 1
    # The analysis was checkpointed: the retry downloaded no log and called no LLM.
    assert len(world.requests("GET", BLOB)) == downloads
    assert len(world.llm.calls) == 1
    assert len(world.requests("POST", CHECKS)) == 1
