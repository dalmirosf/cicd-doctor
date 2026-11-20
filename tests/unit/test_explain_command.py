"""``ExplainCommand`` and its handler: the limit of D-93 and the 👀 of FR-19, on memory adapters."""

import logging
from datetime import timedelta
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import FakeReporter, InMemoryCommandLog
from cicd_doctor.core.errors import CommentGone, PermanentError, TransientError
from cicd_doctor.core.models import ExplainPayload, Job, JobKind, JobStatus
from cicd_doctor.core.usecases.explain_command import ExplainCommand
from cicd_doctor.worker.handlers import explain_handler
from tests.factories import T0

WINDOW = timedelta(minutes=5)


def payload(comment_id: int = 1, *, pr: int = 12, repo: int = 100, actor: str = "ann") -> Any:
    return ExplainPayload(
        installation_id=10,
        repo_id=repo,
        repo_full_name="acme/widgets",
        private=True,
        default_branch="main",
        account_login="acme",
        account_type="Organization",
        pr_number=pr,
        comment_id=comment_id,
        actor_login=actor,
        received_at=T0,
    )


class World:
    def __init__(self) -> None:
        self.clock = FakeClock(T0)
        self.reporter = FakeReporter()
        self.log = InMemoryCommandLog()
        self.command = ExplainCommand(self.log, self.reporter, self.clock, window=WINDOW)


@pytest.fixture
def world() -> World:
    return World()


async def test_an_accepted_command_gets_one_eyes_reaction_and_nothing_else(world: World) -> None:
    assert await world.command.run(payload(7)) is True

    assert world.reporter.reactions == [(7, "eyes")]
    assert world.reporter.methods() == ["react"]  # no check run, no comment
    assert world.reporter.checks == {}
    assert world.reporter.comments == {}


async def test_a_second_command_within_the_window_is_ignored_in_silence(world: World) -> None:
    await world.command.run(payload(1))
    world.clock.advance(WINDOW - timedelta(seconds=1))  # 4:59

    assert await world.command.run(payload(2)) is False

    assert world.reporter.reactions == [(1, "eyes")]
    assert world.reporter.methods() == ["react"]


async def test_a_command_after_the_window_is_accepted(world: World) -> None:
    await world.command.run(payload(1))
    world.clock.advance(WINDOW + timedelta(seconds=1))  # 5:01

    assert await world.command.run(payload(2)) is True

    assert world.reporter.reactions == [(1, "eyes"), (2, "eyes")]


async def test_the_window_is_fixed_from_the_last_accepted_command_not_extended(
    world: World,
) -> None:
    await world.command.run(payload(1))
    world.clock.advance(timedelta(minutes=4))
    assert await world.command.run(payload(2)) is False  # refused at 4:00, extends nothing
    world.clock.advance(timedelta(minutes=1, seconds=1))  # 5:01 after the first

    assert await world.command.run(payload(3)) is True


async def test_a_retry_of_the_same_comment_is_accepted_again(world: World) -> None:
    await world.command.run(payload(1))
    world.clock.advance(timedelta(seconds=30))

    assert await world.command.run(payload(1)) is True

    assert world.reporter.reactions == [(1, "eyes")]  # the reaction is idempotent
    assert world.log.count() == 1  # and the window was not spent twice


async def test_the_limit_is_per_pull_request_and_per_repository(world: World) -> None:
    assert await world.command.run(payload(1, pr=12)) is True
    assert await world.command.run(payload(2, pr=13)) is True  # another PR
    assert await world.command.run(payload(3, pr=12, repo=200)) is True  # another repository
    assert await world.command.run(payload(4, pr=12)) is False


async def test_a_command_withdrawn_before_the_reaction_is_dropped_without_an_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.reporter.fail_next("react", CommentGone("gone"))

    with caplog.at_level(logging.INFO):
        assert await world.command.run(payload(1)) is True

    assert world.reporter.reactions == []
    assert "acme/widgets" not in caplog.text  # ids only


async def test_a_failed_reaction_propagates_and_the_retry_reacts(world: World) -> None:
    world.reporter.fail_next("react", TransientError("502"))
    with pytest.raises(TransientError):
        await world.command.run(payload(1))
    world.clock.advance(timedelta(seconds=30))

    assert await world.command.run(payload(1)) is True

    assert world.reporter.reactions == [(1, "eyes")]


async def test_a_permanent_error_of_the_reaction_propagates(world: World) -> None:
    world.reporter.fail_next("react", PermanentError("Issues: write not accepted"))

    with pytest.raises(PermanentError):
        await world.command.run(payload(1))


# --- the handler ---------------------------------------------------------------------------------


def job(body: dict[str, Any]) -> Job:
    return Job(
        id=1,
        kind=JobKind.EXPLAIN,
        payload=body,
        dedupe_key="explain:1",
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        run_after=T0,
        locked_until=None,
        locked_by=None,
        last_error=None,
    )


async def test_the_handler_runs_the_use_case_on_a_valid_payload(world: World) -> None:
    await explain_handler(world.command)(job(payload(3).model_dump(mode="json")))

    assert world.reporter.reactions == [(3, "eyes")]


async def test_a_malformed_payload_is_a_permanent_error(world: World) -> None:
    with pytest.raises(PermanentError, match="invalid explain payload"):
        await explain_handler(world.command)(job({"comment_id": "x"}))

    assert world.reporter.calls == []


async def test_a_refused_command_ends_without_error(world: World) -> None:
    handler = explain_handler(world.command)
    await handler(job(payload(1).model_dump(mode="json")))
    await handler(job(payload(2).model_dump(mode="json")))

    assert world.reporter.reactions == [(1, "eyes")]
