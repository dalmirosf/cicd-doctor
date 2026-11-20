"""Offline end to end of the command (P3 c, D-92, D-93): a signed ``issue_comment`` webhook becomes
an ``explain`` job; the real worker, use case and ``GitHubReporter`` leave one 👀 reaction on
``FakeGitHub`` and nothing else. No network and no Postgres."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.adapters.github.reporter import GitHubReporter
from cicd_doctor.adapters.memory import InMemoryCommandLog, InMemoryJobQueue
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.core.models import JobKind, JobStatus
from cicd_doctor.core.usecases.explain_command import ExplainCommand
from cicd_doctor.worker import Worker
from cicd_doctor.worker.handlers import explain_handler
from tests.diagnose_world import INSTALLATION_ID, REPO_ID, app_key
from tests.github_fake import FakeGitHub, FakeRepoWrites
from tests.webhooks import SECRET, encode, headers, load

WEBHOOK = "/webhooks/github"
REACTIONS = "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"
COMMAND = "/cicd-doctor explain"
TOKEN = "/app/installations/{installation_id}/access_tokens"


@dataclass
class World:
    clock: FakeClock
    github: FakeGitHub
    writes: FakeRepoWrites
    queue: InMemoryJobQueue
    worker: Worker

    def deliver(self, *, comment_id: int, body: str = COMMAND, **changes: Any) -> int:
        payload = load("issue_comment_created")
        payload["comment"]["id"] = comment_id
        payload["comment"]["body"] = body
        for key, value in changes.items():
            payload["comment"][key] = value
        deps = ApiDependencies(
            queue=self.queue, webhook_secrets=(SECRET,), ready=_ready, clock=self.clock
        )
        raw = encode(payload)
        with TestClient(create_app(deps)) as client:
            return client.post(
                WEBHOOK, content=raw, headers=headers("issue_comment", raw)
            ).status_code

    async def run_job(self) -> bool:
        return await self.worker.run_one("e2e")

    def comment(self, comment_id: int) -> None:
        """The comment exists on GitHub (the fake knows it)."""
        self.writes.comments[comment_id] = {
            "id": comment_id,
            "body": COMMAND,
            "user": {"login": "acme-maintainer", "type": "User"},
            "_repo": "acme/widgets",
            "_pr": 12,
        }


async def _ready() -> bool:
    return True


@pytest.fixture
async def world() -> AsyncIterator[World]:
    clock = FakeClock(datetime.fromisoformat("2026-11-20T12:00:00+00:00"))
    github = FakeGitHub(clock, app_key().public_key())
    github.install(
        INSTALLATION_ID,
        repo_ids={REPO_ID},
        permissions={
            "actions": "read",
            "checks": "write",
            "contents": "read",
            "issues": "write",
            "metadata": "read",
            "pull_requests": "write",
        },
    )
    writes = FakeRepoWrites(github)
    queue = InMemoryJobQueue(clock)
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(GitHubHttp(clock, transport=github.transport()))
        auth = GitHubAppAuth(http, app_id=github.app_id, private_key=app_key(), clock=clock)
        command = ExplainCommand(
            InMemoryCommandLog(),
            GitHubReporter(http, auth, clock),
            clock,
            window=timedelta(minutes=5),
        )
        worker = Worker(
            queue,
            {JobKind.EXPLAIN: explain_handler(command)},
            clock,
            concurrency=1,
            lease=timedelta(minutes=5),
            jitter=lambda: 0.0,
        )
        yield World(clock, github, writes, queue, worker)


async def test_a_command_ends_in_one_eyes_reaction_and_nothing_else(world: World) -> None:
    world.comment(777)

    assert world.deliver(comment_id=777) == 202
    assert await world.run_job() is True

    assert world.writes.reactions == [(777, "eyes")]
    writes = [r for r in world.github.requests if r.method != "GET" and r.template != TOKEN]
    assert [(r.method, r.template) for r in writes] == [("POST", REACTIONS)]
    assert world.writes.check_runs == {}
    posted = [r for r in world.github.requests if r.template and r.template.endswith("/comments")]
    assert posted == []  # no comment either
    job = await world.queue.get(1)
    assert job is not None
    assert job.status is JobStatus.DONE
    assert "/cicd-doctor" not in str(job.payload)  # the body was never stored


async def test_a_second_command_within_five_minutes_is_ignored_in_silence(world: World) -> None:
    world.comment(777)
    world.comment(778)
    assert world.deliver(comment_id=777) == 202
    await world.run_job()
    world.clock.advance(timedelta(minutes=4, seconds=59))

    assert world.deliver(comment_id=778) == 202
    assert await world.run_job() is True

    assert world.writes.reactions == [(777, "eyes")]
    assert len(world.github.requests_to("POST", REACTIONS)) == 1


async def test_a_command_after_five_minutes_is_accepted(world: World) -> None:
    world.comment(777)
    world.comment(778)
    world.deliver(comment_id=777)
    await world.run_job()
    world.clock.advance(timedelta(minutes=5, seconds=1))

    world.deliver(comment_id=778)
    await world.run_job()

    assert world.writes.reactions == [(777, "eyes"), (778, "eyes")]


@pytest.mark.parametrize(
    "changes",
    [
        {"author_association": "CONTRIBUTOR"},
        {"author_association": "NONE"},
        {"user": {"login": "a-bot", "id": 5, "type": "Bot"}},
        {"body": "/cicd-doctor explain please"},
    ],
)
async def test_a_command_that_is_not_authorized_creates_no_job_and_no_reaction(
    world: World, changes: dict[str, Any]
) -> None:
    world.comment(777)

    assert world.deliver(comment_id=777, **changes) == 204

    assert await world.run_job() is False  # no job
    assert world.writes.reactions == []
    assert world.github.requests == []


async def test_a_redelivered_comment_makes_one_job_and_one_reaction(world: World) -> None:
    world.comment(777)
    assert world.deliver(comment_id=777) == 202
    assert world.deliver(comment_id=777) == 200  # another delivery id, same comment: a duplicate
    await world.run_job()
    assert await world.run_job() is False

    assert world.writes.reactions == [(777, "eyes")]


async def test_a_comment_deleted_before_the_worker_arrives_ends_done_without_a_reaction(
    world: World,
) -> None:
    assert world.deliver(comment_id=777) == 202  # GitHub knows no comment 777: it was deleted

    assert await world.run_job() is True

    job = await world.queue.get(1)
    assert job is not None
    assert job.status is JobStatus.DONE
    assert world.writes.reactions == []
