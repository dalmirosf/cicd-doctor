"""``ExplainCommand`` and its handler: the limit of D-93 and the 👀 of FR-19, on memory adapters."""

import logging
from datetime import timedelta
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    FakePullRequests,
    FakeReporter,
    FakeRunSource,
    InMemoryCommandLog,
    InMemoryJobQueue,
)
from cicd_doctor.adapters.memory.repo_config import FakeRepoConfig
from cicd_doctor.core.errors import CommentGone, PermanentError, TransientError
from cicd_doctor.core.models import (
    DiagnoseRunPayload,
    ExplainPayload,
    Job,
    JobKind,
    JobStatus,
    LoadedConfig,
    PullRequestInfo,
    RepoConfig,
    RunSummary,
)
from cicd_doctor.core.usecases.explain_command import ExplainCommand, select_runs
from cicd_doctor.worker.handlers import explain_handler
from tests.factories import T0, make_run_ref

HEAD = "a" * 40

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


def pull(**changes: Any) -> PullRequestInfo:
    fields: dict[str, Any] = {
        "number": 12,
        "state": "open",
        "draft": False,
        "merged": False,
        "head_sha": HEAD,
        "head_branch": "feature/x",
        "head_repo_id": 100,
        "head_repo_full_name": "acme/widgets",
        "base_repo_id": 100,
    }
    return PullRequestInfo(**(fields | changes))


def summary(
    run_id: int,
    *,
    workflow: int = 7,
    conclusion: str | None = "failure",
    status: str = "completed",
    sha: str = HEAD,
    head_repo_id: int | None = 100,
    attempt: int = 1,
    name: str | None = None,
) -> RunSummary:
    return RunSummary(
        run=make_run_ref(
            run_id=run_id,
            run_attempt=attempt,
            workflow_id=workflow,
            workflow_name=name or f"wf{workflow}",
            head_sha=sha,
            is_fork=head_repo_id != 100,
        ),
        status=status,
        conclusion=conclusion,
        updated_at=T0,
        head_repo_id=head_repo_id,
    )


class World:
    def __init__(self, *, llm_enabled: bool = True, max_runs: int = 3) -> None:
        self.clock = FakeClock(T0)
        self.reporter = FakeReporter()
        self.log = InMemoryCommandLog()
        self.pulls = FakePullRequests()
        self.pulls.add(100, pull())
        self.runs = FakeRunSource()
        self.runs.set(HEAD, [summary(5000)])
        self.config = FakeRepoConfig()
        self.queue = InMemoryJobQueue(self.clock)
        self.command = ExplainCommand(
            self.log,
            self.reporter,
            self.pulls,
            self.runs,
            self.config,
            self.queue,
            self.clock,
            window=WINDOW,
            max_runs=max_runs,
            llm_enabled=llm_enabled,
        )

    async def children(self) -> list[Job]:
        found = [await self.queue.get(i) for i in range(1, 20)]
        return [j for j in found if j is not None and j.kind is JobKind.DIAGNOSE_RUN]


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


# --- which runs are explained (D-96) -------------------------------------------------------------


def ids(selected: list[RunSummary]) -> list[int]:
    return [item.run.run_id for item in selected]


def test_the_latest_run_of_each_workflow_wins_and_the_result_is_newest_first() -> None:
    runs = [
        summary(10, workflow=1),
        summary(14, workflow=1),
        summary(12, workflow=2),
        summary(11, workflow=3),
    ]

    assert ids(select_runs(pull(), runs)) == [14, 12, 11]
    assert ids(select_runs(pull(), list(reversed(runs)))) == [14, 12, 11]  # order of listing: any


def test_at_most_the_limit_most_recent_runs_are_selected() -> None:
    runs = [summary(i, workflow=i) for i in range(1, 8)]

    assert ids(select_runs(pull(), runs)) == [7, 6, 5]
    assert ids(select_runs(pull(), runs, limit=1)) == [7]


@pytest.mark.parametrize(
    "item",
    [
        summary(1, status="in_progress", conclusion=None),
        summary(1, status="queued", conclusion=None),
        summary(1, conclusion="success"),
        summary(1, conclusion="cancelled"),
        summary(1, conclusion="skipped"),
        summary(1, conclusion="action_required"),  # a fork run that waits for approval
        summary(1, sha="b" * 40),  # another head commit
        summary(1, head_repo_id=777),  # another head repository (a fork of the same sha)
        summary(1, head_repo_id=None),  # a deleted head repository
    ],
)
def test_runs_that_are_not_a_finished_failure_of_this_head_are_not_selected(
    item: RunSummary,
) -> None:
    assert select_runs(pull(), [item]) == []


def test_a_timed_out_run_is_selected() -> None:
    assert ids(select_runs(pull(), [summary(1, conclusion="timed_out")])) == [1]


def test_a_fork_pull_request_matches_its_fork_runs_by_head_repository() -> None:
    fork = pull(head_repo_id=777, head_repo_full_name="drive-by/widgets")
    runs = [summary(1, head_repo_id=777), summary(2, head_repo_id=100)]

    assert ids(select_runs(fork, runs)) == [1]


def test_a_deleted_head_repository_matches_runs_of_a_deleted_head_repository() -> None:
    gone = pull(head_repo_id=None, head_repo_full_name="")

    assert ids(select_runs(gone, [summary(1, head_repo_id=None), summary(2)])) == [1]


def test_a_newer_run_in_progress_does_not_hide_an_older_failure() -> None:
    runs = [summary(1, workflow=1), summary(2, workflow=1, status="in_progress", conclusion=None)]

    assert ids(select_runs(pull(), runs)) == [1]  # the command does not wait: ask again later


# --- the command end to end, on memory adapters --------------------------------------------------


async def test_every_selected_run_becomes_one_child_job_with_the_explain_trigger(
    world: World,
) -> None:
    world.runs.set(HEAD, [summary(5000, workflow=1), summary(5001, workflow=2, attempt=2)])

    await world.command.run(payload(77, pr=12))

    children = await world.children()
    assert [c.dedupe_key for c in children] == ["explain:77:5001:2", "explain:77:5000:1"]
    first = DiagnoseRunPayload.model_validate(children[0].payload)
    assert first.trigger == "explain"
    assert first.pr_number == 12
    assert first.command_comment_id == 77
    assert first.run.run_id == 5001
    assert first.conclusion == "failure"
    assert first.default_branch == "main"
    assert first.account_login == "acme"
    assert first.completed_at == T0
    assert world.reporter.reactions == [(77, "eyes")]


async def test_the_children_are_capped_by_max_runs() -> None:
    world = World(max_runs=2)
    world.runs.set(HEAD, [summary(i, workflow=i) for i in range(1, 6)])

    await world.command.run(payload(1))

    assert len(await world.children()) == 2


async def test_the_runs_are_asked_for_the_head_the_pull_request_has_now(world: World) -> None:
    world.pulls.add(100, pull(head_sha="c" * 40))
    world.runs.set("c" * 40, [summary(9, sha="c" * 40)])

    await world.command.run(payload(1))

    assert world.runs.calls == [(100, "c" * 40)]
    (child,) = await world.children()
    assert child.payload["run"]["head_sha"] == "c" * 40


async def test_no_failed_run_reacts_confused_and_creates_no_job(world: World) -> None:
    world.runs.set(HEAD, [summary(1, conclusion="success")])

    assert await world.command.run(payload(1)) is True

    assert world.reporter.reactions == [(1, "eyes"), (1, "confused")]
    assert await world.children() == []


async def test_the_workflows_in_ignore_workflows_are_not_explained(world: World) -> None:
    world.runs.set(HEAD, [summary(1, workflow=1, name="Lint"), summary(2, workflow=2, name="CI")])
    world.config.set(LoadedConfig(config=RepoConfig(ignore_workflows=["Lint"]), state="file"))

    await world.command.run(payload(1))

    (child,) = await world.children()
    assert child.payload["run"]["workflow_name"] == "CI"


async def test_an_ignored_workflow_does_not_use_up_the_limit() -> None:
    world = World(max_runs=1)
    world.runs.set(HEAD, [summary(9, workflow=1, name="Lint"), summary(5, workflow=2, name="CI")])
    world.config.set(LoadedConfig(config=RepoConfig(ignore_workflows=["Lint"]), state="file"))

    await world.command.run(payload(1))

    (child,) = await world.children()
    assert child.payload["run"]["run_id"] == 5


async def test_when_every_workflow_is_ignored_the_answer_is_confused(world: World) -> None:
    world.runs.set(HEAD, [summary(1, name="CI")])
    world.config.set(LoadedConfig(config=RepoConfig(ignore_workflows=["CI"]), state="file"))

    await world.command.run(payload(1))

    assert world.reporter.reactions[-1] == (1, "confused")
    assert await world.children() == []


@pytest.mark.parametrize(
    "loaded",
    [
        LoadedConfig(config=RepoConfig(llm="off"), state="file"),
        LoadedConfig(state="invalid", problems=("llm",)),
    ],
)
async def test_llm_off_or_an_invalid_file_reacts_confused_without_listing_runs(
    world: World, loaded: LoadedConfig
) -> None:
    world.config.set(loaded)

    await world.command.run(payload(1))

    assert world.reporter.reactions == [(1, "eyes"), (1, "confused")]
    assert world.runs.calls == []
    assert await world.children() == []


async def test_the_global_kill_switch_reacts_confused_too() -> None:
    world = World(llm_enabled=False)

    await world.command.run(payload(1))

    assert world.reporter.reactions == [(1, "eyes"), (1, "confused")]
    assert await world.children() == []


async def test_on_demand_is_exactly_what_the_command_is_for(world: World) -> None:
    world.config.set(LoadedConfig(config=RepoConfig(llm="on_demand"), state="file"))

    await world.command.run(payload(1))

    assert len(await world.children()) == 1


@pytest.mark.parametrize("changes", [{"state": "closed"}, {"state": "closed", "merged": True}])
async def test_a_closed_or_merged_pull_request_ends_without_runs_or_jobs(
    world: World, changes: dict[str, Any]
) -> None:
    world.pulls.add(100, pull(**changes))

    assert await world.command.run(payload(1)) is True

    assert world.reporter.reactions == [(1, "eyes")]
    assert world.runs.calls == []
    assert await world.children() == []


async def test_a_pull_request_that_does_not_exist_ends_quietly(world: World) -> None:
    assert await world.command.run(payload(1, pr=99)) is True

    assert world.reporter.reactions == [(1, "eyes")]
    assert await world.children() == []


async def test_a_refused_command_reads_nothing_from_github(world: World) -> None:
    await world.command.run(payload(1))
    gets, listings = len(world.pulls.gets), len(world.runs.calls)

    assert await world.command.run(payload(2)) is False

    assert (len(world.pulls.gets), len(world.runs.calls)) == (gets, listings)
    assert len(await world.children()) == 1


async def test_a_comment_deleted_between_the_claim_and_the_reaction_stops_there(
    world: World,
) -> None:
    world.reporter.fail_next("react", CommentGone("gone"))

    assert await world.command.run(payload(1)) is True

    assert world.pulls.gets == []
    assert await world.children() == []


async def test_a_retry_of_the_job_adds_no_second_child_and_no_second_reaction(
    world: World,
) -> None:
    await world.command.run(payload(1))
    world.clock.advance(timedelta(seconds=20))
    await world.command.run(payload(1))  # the worker retried the same comment

    assert len(await world.children()) == 1
    assert world.reporter.reactions == [(1, "eyes")]


async def test_a_failure_listing_the_runs_propagates_and_the_retry_finishes(world: World) -> None:
    world.runs.fail(TransientError("502"))
    with pytest.raises(TransientError):
        await world.command.run(payload(1))
    world.runs.fail(None)

    await world.command.run(payload(1))

    assert len(await world.children()) == 1


async def test_a_failure_reading_the_pull_request_propagates(world: World) -> None:
    world.pulls.fail(TransientError("502"))

    with pytest.raises(TransientError):
        await world.command.run(payload(1))


async def test_a_failure_reading_the_configuration_propagates(world: World) -> None:
    world.config.fail_with(TransientError("502"))

    with pytest.raises(TransientError):
        await world.command.run(payload(1))
    assert await world.children() == []


async def test_the_timings_are_logged_with_ids_only(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.clock.advance(timedelta(seconds=3))
    with caplog.at_level(logging.INFO):
        await world.command.run(payload(1))

    assert "explain acknowledged seconds_since_command=3.0" in caplog.text
    assert "explain queued seconds_since_command=3.0 children=1" in caplog.text
    assert "acme/widgets" not in caplog.text


async def test_a_second_command_explains_the_same_run_again() -> None:
    world = World()
    await world.command.run(payload(1))
    world.clock.advance(WINDOW + timedelta(seconds=1))

    await world.command.run(payload(2))

    keys = [c.dedupe_key for c in await world.children()]
    assert keys == ["explain:1:5000:1", "explain:2:5000:1"]  # never "diagnose:5000:1"
