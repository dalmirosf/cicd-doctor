"""Similar past failures in the diagnosis flow, on in-memory fakes (P2 d, D-82).

Run 5000 fails on a missing ``requests``; later runs fail on a missing ``flask``: not identical
(so the cache of D-81 does not serve them), but close in the embedding space.
"""

from typing import Any

import pytest

from cicd_doctor.adapters.memory import FakeEmbedder
from cicd_doctor.adapters.memory.github import FakeCheck
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.models import RunRef, SimilarFailure
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig
from tests.factories import make_run_ref
from tests.unit.test_diagnose_failed_run import World, log_text, workflow_job

SECOND = 5001


def flask_log(world: World, run: RunRef, job_id: int = 21) -> None:
    """A failed job of ``run`` whose log differs from run 5000 only in the module name."""
    world.logs.add_jobs(run, [workflow_job(job_id)])
    text = log_text().replace("requests", "flask")
    world.logs.add_log(job_id, text)


def last_check(world: World) -> FakeCheck:
    return list(world.reporter.checks.values())[-1]


def count_searches(world: World) -> list[tuple[Any, ...]]:
    searches: list[tuple[Any, ...]] = []
    original = world.store.similar

    async def spy(*args: Any, **kwargs: Any) -> list[SimilarFailure]:
        searches.append((args, kwargs))
        return await original(*args, **kwargs)

    world.store.similar = spy  # type: ignore[method-assign]
    return searches


async def test_a_similar_failure_reaches_the_prompt_and_both_reports() -> None:
    world = World([valid(), valid()], embedder=FakeEmbedder())
    await world.run_once()
    first = (await world.record()).items[0]
    run = make_run_ref(run_id=SECOND)
    flask_log(world, run)

    await world.run_once(run)

    assert world.llm.calls[0].similar == []  # nothing came before the first run
    [similar] = world.llm.calls[1].similar
    assert (similar.failure_id, similar.run_id, similar.run_attempt) == (first.failure.id, 5000, 1)
    assert similar.score >= 0.85
    check = last_check(world).output
    assert check is not None
    assert "[run #5000](https://github.com/acme/widgets/actions/runs/5000)" in check.summary
    assert f"(score {similar.score:.2f})" in check.summary
    comment = world.reporter.comment_bodies[-1]
    assert "**Similar past failure:**" in comment
    assert "run #5000" in comment


async def test_the_search_is_for_this_repository_and_excludes_this_run_before_the_llm() -> None:
    world = World([valid()], embedder=FakeEmbedder())
    searches = count_searches(world)
    seen_before_llm: list[int] = []
    original = world.llm.diagnose

    async def spy(request: Any, *args: Any, **kwargs: Any) -> Any:
        seen_before_llm.append(len(searches))
        return await original(request, *args, **kwargs)

    world.llm.diagnose = spy  # type: ignore[method-assign]

    await world.run_once()

    # One search before the model and one when the report is built; never in the checkpoint.
    assert seen_before_llm == [1]
    assert len(searches) == 2
    args, kwargs = searches[0]
    assert (args[0], args[2], args[3]) == (world.run.repo_id, 3, 0.85)
    assert kwargs == {"exclude_run_id": world.run.run_id}


async def test_a_job_without_an_embedding_has_no_similar_failures() -> None:
    class Down(FakeEmbedder):
        def embed(self, texts: Any) -> Any:
            raise RuntimeError("model not loaded")

    world = World([valid()], embedder=Down())
    searches = count_searches(world)

    await world.run_once()

    assert searches == []
    assert world.llm.calls[0].similar == []


async def test_similar_k_zero_turns_the_feature_off() -> None:
    world = World([valid(), valid()], embedder=FakeEmbedder(), config=DiagnoseConfig(similar_k=0))
    searches = count_searches(world)
    await world.run_once()
    run = make_run_ref(run_id=SECOND)
    flask_log(world, run)

    await world.run_once(run)

    assert searches == []
    assert world.llm.calls[1].similar == []
    assert "Similar past" not in (last_check(world).output.summary)  # type: ignore[union-attr]


async def test_a_failure_of_another_repository_is_never_similar() -> None:
    world = World([valid(), valid()], embedder=FakeEmbedder())
    await world.run_once()
    other = make_run_ref(run_id=SECOND, repo_id=200, repo_full_name="acme/other")
    flask_log(world, other)

    await world.run_once(other)

    assert world.llm.calls[1].similar == []
    check = last_check(world).output
    assert check is not None
    assert "Similar past" not in check.summary


async def test_a_fork_run_shows_similar_failures_but_never_calls_the_model() -> None:
    world = World([valid()], embedder=FakeEmbedder())
    await world.run_once()
    fork = make_run_ref(run_id=SECOND, is_fork=True, head_repo_full_name="someone/widgets")
    flask_log(world, fork)

    await world.run_once(fork)

    assert len(world.llm.calls) == 1  # only run 5000 reached the model
    check = last_check(world).output
    assert check is not None
    assert "run #5000" in check.summary


async def test_a_failure_of_a_fork_is_not_offered_to_later_runs() -> None:
    world = World([valid()], embedder=FakeEmbedder())
    fork = make_run_ref(run_id=5000, is_fork=True, head_repo_full_name="someone/widgets")
    world.add_failed_job(fork, 11)
    await world.run_once(fork)
    run = make_run_ref(run_id=SECOND)
    flask_log(world, run)

    await world.run_once(run)

    assert world.llm.calls[0].similar == []


async def test_a_database_error_is_not_permanent_and_publishes_no_analysis() -> None:
    world = World([valid()], embedder=FakeEmbedder())

    async def broken(*args: Any, **kwargs: Any) -> list[SimilarFailure]:
        raise ConnectionError("database down")

    world.store.similar = broken  # type: ignore[method-assign]

    with pytest.raises(ConnectionError):
        await world.run_once()

    assert await world.store.run_analysis(world.run.run_id, world.run.run_attempt) is None
    assert world.llm.calls == []
