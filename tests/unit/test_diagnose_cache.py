"""The exact-signature cache in the diagnosis flow, on in-memory fakes (P2 c, D-81).

Run 5000 is diagnosed by the LLM; an identical failure in a later run is served from the store:
no LLM call, no budget reservation, ``source = cache``, evidence anchored to the new excerpt.
"""

from datetime import timedelta
from typing import Any

from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.models import CachedDiagnosis, LoadedConfig, RepoConfig, RunRef
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig
from tests.factories import T0, make_run_ref
from tests.unit.test_diagnose_failed_run import World, stamp, workflow_job

SECOND = 5001
OTHER_ERROR = ["Run make", "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"]
SAME_ERROR = ["Run pytest", "ModuleNotFoundError: No module named 'requests'"]


def second_run(
    world: World, extra: tuple[str, ...] = ("pip install -r requirements.txt",)
) -> RunRef:
    """Another run of the same repository with the same failure, on other line numbers."""
    run = make_run_ref(run_id=SECOND)
    world.add_failed_job(run, 21, extra)
    return run


def log_of(lines: list[str]) -> str:
    return "\n".join(
        f"{stamp(T0 + timedelta(seconds=31) + timedelta(milliseconds=100 * i))} {line}"
        for i, line in enumerate(lines)
    )


def count_lookups(world: World) -> list[tuple[int, str]]:
    lookups: list[tuple[int, str]] = []
    original = world.store.cached_diagnosis

    async def spy(*args: Any) -> CachedDiagnosis | None:
        lookups.append((args[0], args[1]))
        return await original(*args)

    world.store.cached_diagnosis = spy  # type: ignore[assignment]
    return lookups


async def test_the_identical_failure_of_the_next_run_is_served_from_the_cache() -> None:
    world = World([valid()])
    await world.run_once()
    first = (await world.record()).items[0]
    today = world.clock.now().date()
    before = world.ledger.usage(today, world.run.installation_id)
    run = second_run(world)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.source == "cache"
    assert item.usage is None
    assert len(world.llm.calls) == 1
    after = world.ledger.usage(today, world.run.installation_id)
    assert before.calls == 1
    assert (after.calls, after.spent_usd) == (before.calls, before.spent_usd)
    # The evidence is a quote of the NEW excerpt, on its own line number.
    [quote] = item.published.evidence
    assert quote.line != first.published.evidence[0].line
    lines = {line.number: line.text for line in item.failure.excerpt.lines}
    assert quote.text in lines[quote.line]
    # The origin is the llm row of run 5000, known by id and by run.
    origin = next(r for r in world.store.diagnosis_rows(first.failure.id) if r.published)
    assert item.cached_from == origin.id
    assert item.cached_from_run_id == 5000
    [row] = [r for r in world.store.diagnosis_rows(item.failure.id) if r.published]
    assert row.cached_from == origin.id


async def test_the_check_and_the_comment_say_which_run_the_answer_came_from() -> None:
    world = World([valid()])
    await world.run_once()
    run = second_run(world)

    await world.run_once(run)

    outputs = [c.output.summary for c in world.reporter.checks.values() if c.output]
    assert any("cached from run #5000" in text for text in outputs)
    [comment] = world.reporter.comments.values()
    assert "cached from run #5000" in comment.body


async def test_a_second_identical_run_makes_exactly_one_reservation_in_the_ledger() -> None:
    reserved: list[int] = []
    world = World([valid()])
    original = world.ledger.reserve

    async def spy(installation_id: int, estimate: Any) -> Any:
        reserved.append(installation_id)
        return await original(installation_id, estimate)

    world.ledger.reserve = spy  # type: ignore[assignment]
    await world.run_once()
    await world.run_once(second_run(world))

    assert len(reserved) == 1
    assert len(world.llm.calls) == 1


async def test_a_fork_with_the_same_signature_gets_no_cache_and_asks_nothing_to_the_store() -> None:
    world = World([valid()])
    await world.run_once()
    lookups = count_lookups(world)
    run = make_run_ref(run_id=SECOND, is_fork=True, head_repo_full_name="evil/widgets")
    world.add_failed_job(run, 21)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.source == "rules"
    assert item.published.fallback_reason == "fork_policy"
    assert item.cached_from is None
    assert lookups == []
    assert len(world.llm.calls) == 1


async def test_with_the_kill_switch_on_the_store_is_not_asked_for_a_cache() -> None:
    world = World([valid()])
    await world.run_once()
    world.usecase._config = DiagnoseConfig(llm_enabled=False)
    lookups = count_lookups(world)
    run = second_run(world)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.fallback_reason == "disabled"
    assert lookups == []


async def test_with_llm_off_in_the_repository_the_store_is_not_asked_for_a_cache() -> None:
    world = World([valid()])
    await world.run_once()
    world.repo_config.set(LoadedConfig(config=RepoConfig(llm="off"), state="file"))
    lookups = count_lookups(world)
    run = second_run(world)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.fallback_reason == "config_off"
    assert lookups == []


async def test_a_ttl_of_zero_turns_the_cache_off() -> None:
    world = World([valid(), valid()], config=DiagnoseConfig(cache_ttl_days=0))
    lookups = count_lookups(world)
    await world.run_once()

    await world.run_once(second_run(world))

    assert len(world.llm.calls) == 2
    assert lookups == []


async def test_an_expired_entry_calls_the_llm_again() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    world.clock.advance(timedelta(days=8))
    run = second_run(world)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.source == "llm"
    assert len(world.llm.calls) == 2


async def test_the_entry_is_still_served_just_inside_the_ttl() -> None:
    world = World([valid()])
    await world.run_once()
    world.clock.advance(timedelta(days=6, hours=23))
    run = second_run(world)

    await world.run_once(run)

    assert (await world.record(run)).items[0].published.source == "cache"
    assert len(world.llm.calls) == 1


async def test_another_signature_does_not_hit() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    run = make_run_ref(run_id=SECOND)
    world.logs.add_jobs(run, [workflow_job(21)])
    world.logs.add_log(21, log_of(OTHER_ERROR))

    await world.run_once(run)

    [item] = (await world.record(run)).items
    first = (await world.record()).items[0]
    assert item.failure.signature.hash != first.failure.signature.hash
    assert item.published.source == "llm"
    assert len(world.llm.calls) == 2


async def test_a_cached_answer_of_another_repository_does_not_hit() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    other = make_run_ref(run_id=SECOND, repo_id=200, repo_full_name="acme/gadgets")
    world.add_failed_job(other, 21)

    await world.run_once(other)

    [item] = (await world.record(other)).items
    assert item.published.source == "llm"
    assert len(world.llm.calls) == 2


async def test_when_no_evidence_can_be_anchored_the_normal_policy_calls_the_llm() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    # Same signature, but the stored evidence quotes text the new excerpt does not have.
    [first] = (await world.record()).items
    [stored] = [r for r in world.store.diagnosis_rows(first.failure.id) if r.published]
    stale = stored.diagnosis.model_copy(
        update={"evidence": [stored.diagnosis.evidence[0].model_copy(update={"text": "gone"})]}
    )
    object.__setattr__(stored, "diagnosis", stale)
    run = second_run(world)

    await world.run_once(run)

    [item] = (await world.record(run)).items
    assert item.published.source == "llm"
    assert len(world.llm.calls) == 2


async def test_the_jobs_of_a_matrix_with_one_signature_each_call_the_llm_once() -> None:
    """The known limit (D-81): the siblings of one attempt are looked up before any of them is
    stored, so every job calls the model. The cache serves the next run, not the siblings."""
    world = World([valid()] * 3, jobs=False)
    run = make_run_ref()
    world.logs.add_jobs(
        run,
        [
            workflow_job(11, name="tests (3.11)"),
            workflow_job(12, name="tests (3.12)"),
            workflow_job(13, name="tests (3.13)"),
        ],
    )
    for job_id in (11, 12, 13):
        world.logs.add_log(job_id, log_of(SAME_ERROR))

    await world.run_once()

    items = (await world.record()).items
    assert len({item.failure.signature.hash for item in items}) == 1
    assert [item.published.source for item in items] == ["llm"] * 3
    assert len(world.llm.calls) == 3

    next_run = make_run_ref(run_id=SECOND)
    world.logs.add_jobs(
        next_run, [workflow_job(21, name="tests (3.11)"), workflow_job(22, name="tests (3.12)")]
    )
    for job_id in (21, 22):
        world.logs.add_log(job_id, log_of(SAME_ERROR))

    await world.run_once(next_run)

    assert [i.published.source for i in (await world.record(next_run)).items] == ["cache"] * 2
    assert len(world.llm.calls) == 3
