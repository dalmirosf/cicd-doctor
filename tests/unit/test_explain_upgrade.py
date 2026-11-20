"""``/cicd-doctor explain`` in the diagnosis use case (P3 d1, D-94, D-95), on in-memory fakes.

The command improves what is published: a rules diagnosis of an analysed attempt becomes an LLM
one, in the same check run and the same sticky comment, with no log downloaded. A fork run gets
the model only this way, and nothing of the history (cache, similar failures) reaches it.
"""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from cicd_doctor.adapters.memory import FakeEmbedder
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import (
    CachedDiagnosis,
    Category,
    Diagnosis,
    LoadedConfig,
    RepoConfig,
    RunRef,
    SimilarFailure,
)
from tests.factories import make_run_ref
from tests.unit.test_diagnose_failed_run import World

PR = 12


def fork_run(**changes: Any) -> RunRef:
    return make_run_ref(
        is_fork=True,
        pr_number=None,
        head_repo_full_name="drive-by/widgets",
        head_branch="patch-1",
        **changes,
    )


async def explain(world: World, run: RunRef | None = None, **changes: Any) -> None:
    await world.run_once(run, trigger="explain", pr_number=PR, command_comment_id=99, **changes)


async def sources(world: World) -> list[str]:
    return [item.published.source for item in (await world.record()).items]


async def published_rows(world: World) -> int:
    record = await world.record()
    return sum(
        1
        for item in record.items
        for row in world.store.diagnosis_rows(item.failure.id)
        if row.published
    )


# --- a fork run with a rules checkpoint -----------------------------------------------------


async def test_explain_upgrades_the_published_diagnosis_without_downloading_a_log() -> None:
    world = World(script=[], run=fork_run())
    world.pulls.set(5000, PR)
    await world.run_once()
    assert await sources(world) == ["rules"]
    [check] = world.reporter.checks.values()
    [comment] = world.reporter.comments.values()
    assert check.output is not None
    before_summary = check.output.summary
    before_body = comment.body
    downloads = world.logs.downloads
    world.llm.add(valid())

    await explain(world)

    assert await sources(world) == ["llm"]
    assert len(world.llm.calls) == 1
    assert world.logs.downloads == downloads  # nothing is downloaded again
    assert list(world.reporter.checks) == [check.id]  # the same check run
    assert check.output is not None
    assert check.output.summary != before_summary
    assert list(world.reporter.comments) == [comment.id]  # the same sticky comment
    assert comment.body != before_body
    assert comment.edits == 1
    assert world.reporter.methods().count("start_check") == 1  # never put back in progress
    assert await published_rows(world) == 1


async def test_the_cost_of_the_upgrade_is_kept_on_the_published_row() -> None:
    world = World(script=[], run=fork_run())
    await world.run_once()
    world.llm.add(valid())

    await explain(world)

    [item] = (await world.record()).items
    assert item.usage is not None
    assert item.usage.calls == 1
    assert item.usage.cost_usd > Decimal(0)
    assert item.rules.source == "rules"
    assert item.rules.fallback_reason is None


async def test_a_second_explain_costs_nothing() -> None:
    world = World(script=[], run=fork_run())
    await world.run_once()
    world.llm.add(valid())
    await explain(world)
    spent = world.calls_made()

    await explain(world)

    assert world.calls_made() == spent
    assert await sources(world) == ["llm"]


async def test_a_retry_after_the_upgrade_calls_nothing_again() -> None:
    """The upgrade is stored before it is published: a failure while publishing is retried
    from the store, with no new call and no new reservation."""
    world = World(script=[], run=fork_run())
    world.pulls.set(5000, PR)
    await world.run_once()
    world.llm.add(valid())
    world.store.fail_comment_saves = 1
    with pytest.raises(TransientError):
        await explain(world)
    spent = world.calls_made()
    assert len(world.llm.calls) == 1

    await explain(world)

    assert world.calls_made() == spent
    [comment] = world.reporter.comments.values()
    assert "LLM" in comment.body


async def test_a_diagnosis_that_already_came_from_the_llm_is_not_analysed_again() -> None:
    world = World()  # a normal run: the LLM answers the first time
    await world.run_once()
    spent = world.calls_made()

    await explain(world)

    assert world.calls_made() == spent
    assert await sources(world) == ["llm"]


async def test_an_exhausted_budget_keeps_the_rules_diagnosis_and_breaks_nothing() -> None:
    world = World(script=[], run=fork_run(), daily_budget=Decimal(0))
    world.pulls.set(5000, PR)
    await world.run_once()
    [comment] = world.reporter.comments.values()
    body = comment.body

    await explain(world)

    [item] = (await world.record()).items
    assert item.published.source == "rules"
    assert item.published.fallback_reason == "fork_policy"  # its reason, untouched
    assert world.llm.calls == []
    assert comment.body == body
    assert await published_rows(world) == 1


async def test_llm_off_in_the_repository_never_calls_the_model() -> None:
    world = World(script=[], run=fork_run())
    await world.run_once()
    world.repo_config.set(LoadedConfig(config=RepoConfig(llm="off")))

    await explain(world)

    assert world.llm.calls == []
    assert await sources(world) == ["rules"]


async def test_an_llm_error_keeps_the_rules_diagnosis() -> None:
    from cicd_doctor.adapters.memory.llm import Fail
    from cicd_doctor.core.errors import LLMError

    world = World(script=[], run=fork_run())
    await world.run_once()
    world.llm.add(Fail(LLMError("boom")))

    await explain(world)

    assert await sources(world) == ["rules"]
    assert await published_rows(world) == 1


# --- no checkpoint yet ----------------------------------------------------------------------


async def test_without_a_checkpoint_explain_downloads_the_logs_and_stores_the_analysis() -> None:
    world = World(script=[valid()], run=fork_run())

    await explain(world)

    assert world.logs.downloads == 1
    assert await sources(world) == ["llm"]
    [check] = world.reporter.checks.values()
    assert check.status == "completed"


async def test_the_comment_goes_to_the_pull_request_of_the_command_when_the_run_has_none() -> None:
    world = World(script=[valid()], run=fork_run())  # GitHub links no pull request to a fork run

    await explain(world)

    [comment] = world.reporter.comments.values()
    assert comment.body.splitlines()[0] == f"<!-- cicd-doctor:v1 pr={PR} workflow=7 -->"
    assert (await world.record()).run.pr_number is None  # the run itself is not rewritten


async def test_an_automatic_job_and_an_explain_racing_leave_one_checkpoint() -> None:
    world = World(script=[valid()], run=fork_run())
    world.pulls.set(5000, PR)

    await asyncio.gather(world.run_once(), explain(world))

    record = await world.record()
    assert len(record.items) == 1
    assert await published_rows(world) == 1
    assert len(world.reporter.checks) == 1
    assert len(world.reporter.comments) == 1


async def test_two_explains_of_one_attempt_call_the_model_once() -> None:
    world = World(script=[], run=fork_run())
    world.pulls.set(5000, PR)
    await world.run_once()
    world.llm.add(valid(), valid())

    await asyncio.gather(explain(world), explain(world))

    assert len(world.llm.calls) == 1
    assert await published_rows(world) == 1


# --- the guards -----------------------------------------------------------------------------


async def test_a_newer_attempt_that_published_makes_explain_do_nothing() -> None:
    world = World(script=[], run=fork_run())
    await world.run_once()
    newer = fork_run(run_attempt=2)
    world.add_failed_job(newer, 21)
    await world.run_once(newer)
    world.llm.add(valid())

    await explain(world)  # the command named attempt 1, which is not the published one anymore

    assert world.llm.calls == []


async def test_the_ignored_workflows_are_ignored_by_explain_too() -> None:
    world = World(script=[], run=fork_run())
    await world.run_once()
    world.repo_config.set(LoadedConfig(config=RepoConfig(ignore_workflows=["CI"])))
    world.llm.add(valid())

    await explain(world)

    assert world.llm.calls == []


async def test_comment_false_updates_the_check_and_leaves_the_existing_comment() -> None:
    world = World(script=[], run=fork_run())
    world.pulls.set(5000, PR)
    await world.run_once()
    [comment] = world.reporter.comments.values()
    body = comment.body
    [check] = world.reporter.checks.values()
    world.repo_config.set(LoadedConfig(config=RepoConfig(comment=False)))
    world.llm.add(valid())

    await explain(world)

    assert await sources(world) == ["llm"]
    assert comment.body == body
    assert check.output is not None
    assert "LLM" in check.output.summary


# --- D-94: what a fork run never gets ---------------------------------------------------------


def spy(world: World, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count what the use case asks the store for, and make every search find something."""
    counts = {"cached": 0, "similar": 0}
    origin = uuid4()

    async def cached_diagnosis(*args: Any, **kwargs: Any) -> CachedDiagnosis:
        counts["cached"] += 1
        return CachedDiagnosis(
            diagnosis=Diagnosis(
                category=Category.UNKNOWN,
                probable_cause="from the cache",
                evidence=[],
                suggested_fix="",
                confidence=0.5,
                source="llm",
                model="m",
                prompt_version="x",
            ),
            origin_id=origin,
        )

    async def similar(*args: Any, **kwargs: Any) -> list[SimilarFailure]:
        counts["similar"] += 1
        return [
            SimilarFailure(
                failure_id=uuid4(),
                run_id=1,
                run_attempt=1,
                score=0.99,
                category=Category.DEPENDENCY,
                probable_cause="an earlier cause",
                fix_commit_sha=None,
            )
        ]

    monkeypatch.setattr(world.store, "cached_diagnosis", cached_diagnosis)
    monkeypatch.setattr(world.store, "similar", similar)
    return counts


async def test_a_fork_explain_never_reads_the_cache_and_sends_no_similar_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(script=[], run=fork_run(), embedder=FakeEmbedder())
    await world.run_once()
    counts = spy(world, monkeypatch)
    world.llm.add(valid())

    await explain(world)

    assert counts["cached"] == 0
    [request] = world.llm.calls
    assert request.similar == []
    assert await sources(world) == ["llm"]  # not "cache"


async def test_the_same_explain_on_a_normal_run_does_read_both_the_cache_and_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control of the test above: the spy sees the reads when nothing forbids them."""
    world = World(script=[], embedder=FakeEmbedder())
    world.repo_config.set(LoadedConfig(config=RepoConfig(llm="on_demand")))
    await world.run_once()
    assert await sources(world) == ["rules"]
    counts = spy(world, monkeypatch)  # its cache hit cannot be re-anchored: the model is asked
    world.llm.add(valid())

    await explain(world)

    assert counts["cached"] == 1
    assert len(world.llm.calls) == 1
    assert len(world.llm.calls[0].similar) == 1
    assert await sources(world) == ["llm"]


async def test_the_report_of_a_fork_still_lists_the_similar_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(script=[], run=fork_run(), embedder=FakeEmbedder())
    world.pulls.set(5000, PR)
    await world.run_once()
    spy(world, monkeypatch)
    world.llm.add(valid())

    await explain(world)

    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert "an earlier cause" in check.output.summary


async def test_the_similar_failures_do_reach_the_prompt_of_a_normal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(script=[], embedder=FakeEmbedder())
    world.repo_config.set(LoadedConfig(config=RepoConfig(llm="on_demand")))
    await world.run_once()
    counts = spy(world, monkeypatch)
    # No cache hit this time, so the model is asked.
    monkeypatch.setattr(world.store, "cached_diagnosis", _no_cache)
    world.llm.add(valid())

    await explain(world)

    [request] = world.llm.calls
    assert len(request.similar) == 1
    assert counts["similar"] >= 1


async def _no_cache(*args: Any, **kwargs: Any) -> None:
    return None
