"""The repository configuration in the flows (D-90, D-91), on in-memory fakes."""

from typing import Any

import pytest

from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.adapters.memory.repo_config import FakeRepoConfig
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import LoadedConfig, RepoConfig
from cicd_doctor.core.render import FALLBACK_TEXT
from cicd_doctor.core.repo_config import parse_repo_config
from cicd_doctor.core.usecases.record_run_outcome import RecordRunOutcome
from tests.factories import make_run_ref
from tests.unit.test_diagnose_failed_run import World
from tests.unit.test_record_outcome import outcome_payload, success_run


def file_with(**fields: Any) -> LoadedConfig:
    return LoadedConfig(config=RepoConfig(**fields), state="file")


def outcome_use_case(world: World, source: FakeRepoConfig | None = None) -> RecordRunOutcome:
    return RecordRunOutcome(
        store=world.store,
        installations=world.installations,
        reporter=world.reporter,
        repo_config=source or world.repo_config,
    )


# --- the diagnosis -------------------------------------------------------------------------------


async def test_without_a_file_nothing_changes() -> None:
    world = World()

    await world.run_once()

    assert world.calls_made() == (1, 1, 1)
    assert len(world.reporter.comments) == 1
    [item] = (await world.record()).items
    assert item.published.source == "llm"
    assert len(world.repo_config.calls) == 1


async def test_the_configuration_is_read_once_per_job_and_before_any_write() -> None:
    world = World()
    seen: list[list[str]] = []
    original = world.repo_config.load

    async def spy(repo: Any) -> LoadedConfig:
        seen.append(list(world.journal))
        return await original(repo)

    world.repo_config.load = spy  # type: ignore[method-assign]

    await world.run_once()

    assert seen == [[]]  # no GitHub call had happened yet


async def test_llm_off_in_the_file_publishes_the_rules_diagnosis() -> None:
    world = World()
    world.repo_config.set(file_with(llm="off"))

    await world.run_once()

    [item] = (await world.record()).items
    assert (item.published.source, item.published.fallback_reason) == ("rules", "config_off")
    assert world.calls_made() == (0, 1, 0)


async def test_llm_on_demand_without_explain_says_so() -> None:
    world = World()
    world.repo_config.set(file_with(llm="on_demand"))

    await world.run_once()

    [item] = (await world.record()).items
    assert item.published.fallback_reason == "on_demand"
    assert world.calls_made() == (0, 1, 0)
    assert "explain" in FALLBACK_TEXT["on_demand"]
    assert "disabled" not in FALLBACK_TEXT["on_demand"]
    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert "LLM on demand" in check.output.summary


async def test_an_invalid_file_turns_the_llm_off_and_the_check_names_the_fields() -> None:
    world = World()
    world.repo_config.set(parse_repo_config("llm: sometimes\nunknown: 1\n"))

    await world.run_once()

    [item] = (await world.record()).items
    assert (item.published.source, item.published.fallback_reason) == ("rules", "config_off")
    assert world.calls_made() == (0, 1, 0)
    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert "repository configuration is invalid" in check.output.summary
    assert "`llm`" in check.output.summary
    assert "`unknown`" in check.output.summary
    assert "LLM turned off" in check.output.summary
    assert "sometimes" not in check.output.summary


async def test_a_valid_file_adds_no_configuration_line() -> None:
    world = World()
    world.repo_config.set(file_with(llm="off"))

    await world.run_once()

    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert "configuration is invalid" not in check.output.summary


async def test_an_unreadable_configuration_behaves_as_the_defaults() -> None:
    world = World()
    world.repo_config.set(LoadedConfig(state="unreadable"))

    await world.run_once()

    [item] = (await world.record()).items
    assert item.published.source == "llm"


async def test_comment_false_publishes_the_check_and_no_comment() -> None:
    world = World()
    world.repo_config.set(file_with(comment=False))

    await world.run_once()

    assert len(world.reporter.checks) == 1
    assert world.reporter.comments == {}
    assert "reporter.upsert_comment" not in world.journal


async def test_comment_false_leaves_a_comment_published_before_as_it_is() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    [comment] = world.reporter.comments.values()
    body, edits = comment.body, comment.edits
    world.repo_config.set(file_with(comment=False))
    later = make_run_ref(run_id=5002, head_sha="c" * 40)
    world.add_failed_job(later, 21)

    await world.run_once(later)

    [same] = world.reporter.comments.values()
    assert (same.body, same.edits) == (body, edits)
    assert len(world.reporter.checks) == 2  # the new run still got its check


async def test_an_ignored_workflow_writes_nothing_and_reserves_nothing() -> None:
    world = World()
    world.repo_config.set(file_with(ignore_workflows=["CI"]))

    await world.run_once()

    assert world.journal == []
    assert world.reporter.checks == {}
    assert world.reporter.comments == {}
    assert world.calls_made() == (0, 0, 0)
    assert await world.store.run_analysis(world.run.run_id, world.run.run_attempt) is None
    assert await world.store.publication(world.check_key()) is None


async def test_another_workflow_is_not_ignored() -> None:
    world = World()
    world.repo_config.set(file_with(ignore_workflows=["Nightly", "ci"]))

    await world.run_once()

    assert len(world.reporter.checks) == 1


async def test_a_transient_error_reading_the_file_retries_and_never_uses_the_defaults() -> None:
    world = World()
    world.repo_config.fail_with(TransientError("GitHub is down"))

    with pytest.raises(TransientError):
        await world.run_once()

    assert world.journal == []
    assert world.calls_made() == (0, 0, 0)

    await world.run_once()  # the retry reads the file again
    assert len(world.reporter.checks) == 1


async def test_a_use_case_built_without_a_source_uses_the_defaults() -> None:
    world = World()
    world.usecase._repo_config = None

    await world.run_once()

    assert world.calls_made() == (1, 1, 1)


# --- the resolution rewrite (D-91) ---------------------------------------------------------------


async def test_a_resolution_rewrites_the_comment_when_comments_are_on() -> None:
    world = World()
    await world.run_once()

    await outcome_use_case(world).run(outcome_payload(success_run()))

    [comment] = world.reporter.comments.values()
    assert comment.edits == 1
    assert "Fixed by" in comment.body


async def test_a_resolution_does_not_rewrite_a_comment_when_comment_is_false() -> None:
    world = World()
    await world.run_once()
    [comment] = world.reporter.comments.values()
    body = comment.body
    world.repo_config.set(file_with(comment=False))

    await outcome_use_case(world).run(outcome_payload(success_run()))

    assert (comment.body, comment.edits) == (body, 0)
    failure = (await world.record()).items[0].failure
    assert failure.status.value != "open"  # the success was still recorded and resolved it


async def test_a_resolution_does_not_rewrite_the_comment_of_an_ignored_workflow() -> None:
    world = World()
    await world.run_once()
    [comment] = world.reporter.comments.values()
    world.repo_config.set(file_with(ignore_workflows=["CI"]))

    await outcome_use_case(world).run(outcome_payload(success_run()))

    assert comment.edits == 0


async def test_a_success_with_nothing_to_resolve_does_not_read_the_configuration() -> None:
    world = World()

    await outcome_use_case(world).run(outcome_payload(success_run()))

    assert world.repo_config.calls == []


async def test_a_transient_error_reading_the_file_retries_the_resolution() -> None:
    world = World()
    await world.run_once()
    world.repo_config.fail_with(TransientError("GitHub is down"))
    use_case = outcome_use_case(world)

    with pytest.raises(TransientError):
        await use_case.run(outcome_payload(success_run()))
    await use_case.run(outcome_payload(success_run()))

    [comment] = world.reporter.comments.values()
    assert "Fixed by" in comment.body
