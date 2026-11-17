"""The success path on in-memory fakes (P2 e2a and e2b, D-83, D-84): a success is stored and
resolves the open failures it fixes, and a failure that arrives after its success is resolved.

Only the comment is rewritten (D-84): the check run is never touched by a resolution.
"""

from typing import Any

import pytest

from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import FailureStatus, RecordOutcomePayload, RunRef
from cicd_doctor.core.usecases.record_run_outcome import RecordRunOutcome
from tests.factories import T0, make_run_ref
from tests.unit.test_diagnose_failed_run import World

A, B = "a" * 40, "b" * 40
FLAKY = FailureStatus.RESOLVED_FLAKY
FIX = FailureStatus.RESOLVED_BY_COMMIT
OPEN = FailureStatus.OPEN


def success_run(run_id: int = 5001, attempt: int = 1, sha: str = B, **changes: Any) -> RunRef:
    return make_run_ref(run_id=run_id, run_attempt=attempt, head_sha=sha, **changes)


def outcome_payload(run: RunRef, **changes: Any) -> RecordOutcomePayload:
    values: dict[str, Any] = {
        "run": run,
        "completed_at": T0,
        "default_branch": "main",
        "private": False,
        "account_login": "acme",
        "account_type": "Organization",
    }
    return RecordOutcomePayload(**{**values, **changes})


def use_case(world: World) -> RecordRunOutcome:
    return RecordRunOutcome(
        store=world.store, installations=world.installations, reporter=world.reporter
    )


async def record_success(world: World, run: RunRef, **changes: Any) -> None:
    await use_case(world).run(outcome_payload(run, **changes))


async def failure_of(world: World, run: RunRef | None = None) -> Any:
    [item] = (await world.record(run)).items
    return item.failure


async def test_a_rerun_without_code_changes_resolves_the_failure_as_flaky() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run(run_id=5000, attempt=2, sha=A))

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FLAKY, None)
    [found] = await world.store.resolved_by(world.run.repo_id, 5000, 2)
    assert found.id == failure.id


async def test_a_success_on_another_commit_resolves_the_failure_by_commit() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run())

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    assert [f.id for f in await world.store.resolved_by(world.run.repo_id, 5001, 1)] == [failure.id]


@pytest.mark.parametrize(
    ("name", "success"),
    [
        ("another branch", success_run(head_branch="other")),
        (
            "a fork with the same branch name",
            success_run(is_fork=True, head_repo_full_name="mallory/widgets"),
        ),
        ("another workflow", success_run(workflow_id=8)),
        ("another run on the same commit", success_run(sha=A)),
        ("a run with no branch", success_run(head_branch=None)),
        ("a run older than the failure", success_run(run_id=4999)),
    ],
)
async def test_a_success_that_does_not_fix_it_leaves_the_failure_open(
    name: str, success: RunRef
) -> None:
    world = World()
    await world.run_once()

    await record_success(world, success)

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (OPEN, None)
    assert await world.store.resolved_by(world.run.repo_id, success.run_id, 1) == []


async def test_a_success_in_another_repository_never_resolves_this_one() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run(repo_id=200, repo_full_name="acme/other"))

    assert (await failure_of(world)).status is OPEN


async def test_a_success_with_no_open_failures_makes_no_call_to_github() -> None:
    world = World()
    calls_before = list(world.journal)

    await record_success(world, success_run())

    assert world.journal == calls_before == []
    assert world.reporter.checks == {}
    assert world.reporter.comments == {}


FLAKY_TEXT = "✅ Passed on re-run without code changes — recorded as flaky"


def only_comment(world: World) -> Any:
    [comment] = world.reporter.comments.values()
    return comment


async def test_a_fix_rewrites_the_same_comment_with_the_banner_and_the_compare_link() -> None:
    world = World()
    await world.run_once()
    comment_before = only_comment(world)
    id_before, calls_before = comment_before.id, len(world.reporter.calls)

    await record_success(world, success_run())

    comment = only_comment(world)  # the same comment, edited: still exactly one
    assert comment.id == id_before
    assert comment.edits == 1
    assert "**✅ Fixed by `bbbbbbb`**" in comment.body
    assert "Likely cause" not in comment.body
    assert f"/compare/{A}...{B})" in comment.body
    assert "**Probable cause:**" in comment.body  # the diagnosis stays below the banner
    assert [c.method for c in world.reporter.calls[calls_before:]] == ["upsert_comment"]
    pub = await world.store.publication(world.comment_key())
    assert pub is not None
    assert (pub.run_id, pub.attempt, pub.external_id) == (5001, 1, id_before)


async def test_a_flaky_pass_rewrites_the_comment_with_the_flaky_message() -> None:
    world = World()
    await world.run_once()

    await record_success(world, success_run(run_id=5000, attempt=2, sha=A))

    comment = only_comment(world)
    assert f"**{FLAKY_TEXT}**" in comment.body
    assert "Compare the changes" not in comment.body


async def test_the_check_run_is_never_touched_when_a_failure_is_resolved() -> None:
    world = World()
    await world.run_once()
    [check] = world.reporter.checks.values()
    output = check.output
    calls_before = len(world.reporter.calls)

    await record_success(world, success_run())

    assert check.output == output
    new_calls = [c.method for c in world.reporter.calls[calls_before:]]
    assert "start_check" not in new_calls
    assert "complete_check" not in new_calls


async def test_a_failure_with_no_comment_yet_writes_nothing_and_the_diagnosis_does() -> None:
    world = World()
    await record_success(world, success_run())  # nothing to resolve, nothing to write
    assert world.reporter.calls == []

    await world.run_once()  # the late failure: its own comment is already resolved

    comment = only_comment(world)
    assert "**✅ Fixed by `bbbbbbb`**" in comment.body
    assert "Likely cause" not in comment.body
    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert check.output.title == "✅ Fixed by bbbbbbb"


async def test_a_comment_already_written_by_a_later_run_is_not_overwritten() -> None:
    world = World([valid(), valid()])
    await world.run_once()
    later = make_run_ref(run_id=5002, head_sha="c" * 40)
    world.add_failed_job(later, 21)
    await world.run_once(later)  # the comment now shows run 5002
    body = only_comment(world).body
    edits = only_comment(world).edits

    await record_success(world, success_run(run_id=5001, sha=B))  # resolves 5000, not 5002

    assert (await failure_of(world)).status is FIX
    assert (await failure_of(world, later)).status is OPEN
    assert only_comment(world).body == body
    assert only_comment(world).edits == edits
    pub = await world.store.publication(world.comment_key())
    assert pub is not None
    assert pub.run_id == 5002


async def test_a_retry_after_resolve_and_before_the_comment_writes_the_comment() -> None:
    world = World()
    await world.run_once()
    world.reporter.fail_next("upsert_comment", TransientError("github is down"))

    with pytest.raises(TransientError):
        await record_success(world, success_run())

    assert (await failure_of(world)).status is FIX  # resolved, but the comment is still red
    assert "Likely cause" in only_comment(world).body

    await record_success(world, success_run())  # the retry of the job

    assert "**✅ Fixed by `bbbbbbb`**" in only_comment(world).body
    assert only_comment(world).edits == 1


async def test_a_retry_after_the_comment_was_written_writes_nothing_more() -> None:
    world = World()
    await world.run_once()
    await record_success(world, success_run())
    calls = len(world.reporter.calls)

    await record_success(world, success_run())

    assert len(world.reporter.calls) == calls


async def test_a_resolved_failure_without_a_pull_request_writes_no_comment() -> None:
    run = make_run_ref(pr_number=None)
    world = World(run=run)
    await world.run_once()
    calls = len(world.reporter.calls)

    await record_success(world, success_run(pr_number=None))

    assert (await failure_of(world, run)).status is FIX
    assert len(world.reporter.calls) == calls


async def test_a_success_that_arrives_between_the_report_and_the_comment_is_not_lost() -> None:
    """The diagnosis read no success, then one is recorded and resolves the failure while the
    comment is still unpublished: the success finds no comment to rewrite, so the diagnosis must
    write the resolved state (it reads the record again under the comment lock)."""
    world = World()
    original = world.store.later_successes
    done = False

    async def spy(*args: Any) -> Any:
        nonlocal done
        found = await original(*args)
        if not done:
            done = True
            await record_success(world, success_run())
        return found  # empty: it was read before the success was recorded

    world.store.later_successes = spy  # type: ignore[method-assign,assignment]

    await world.run_once()

    assert (await failure_of(world)).status is FIX
    assert "**✅ Fixed by `bbbbbbb`**" in only_comment(world).body
    assert "Likely cause" not in only_comment(world).body


async def test_a_retry_changes_nothing() -> None:
    world = World()
    await world.run_once()
    await record_success(world, success_run())
    first = await failure_of(world)

    await record_success(world, success_run())

    assert await failure_of(world) == first
    assert len(await world.store.resolved_by(world.run.repo_id, 5001, 1)) == 1


async def test_record_success_does_not_erase_the_pull_request_a_diagnosis_found() -> None:
    world = World()
    await world.run_once()
    assert (await world.record()).run.pr_number == 12

    # The webhook of a fork run has no pull request (D-72): its success must not erase it.
    await record_success(world, success_run(run_id=5000, attempt=2, sha=A, pr_number=None))

    assert (await world.record()).run.pr_number == 12


async def test_the_success_creates_the_installation_and_the_repository() -> None:
    world = World(jobs=False)

    await record_success(world, success_run(), private=True)

    installation = await world.installations.installation(1)
    assert installation is not None
    assert installation.account_login == "acme"


# --- a failure that arrives after its success (D-83) -----------------------------------------


async def test_a_late_failure_is_resolved_by_the_success_that_came_first() -> None:
    world = World()
    await record_success(world, success_run())  # the success of run 5001 arrives first

    await world.run_once()  # the diagnosis of run 5000 arrives after

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    assert [f.id for f in await world.store.resolved_by(world.run.repo_id, 5001, 1)] == [failure.id]


async def test_a_late_failure_of_an_earlier_attempt_is_flaky() -> None:
    world = World()
    await record_success(world, success_run(run_id=5000, attempt=2, sha=A))

    await world.run_once()

    assert (await failure_of(world)).status is FLAKY


async def test_a_late_failure_is_not_resolved_by_a_success_of_the_same_commit() -> None:
    world = World()
    await record_success(world, success_run(sha=A))

    await world.run_once()

    assert (await failure_of(world)).status is OPEN


async def test_a_failure_newer_than_the_success_stays_open() -> None:
    world = World()
    await record_success(world, success_run(run_id=4000))

    await world.run_once()

    assert (await failure_of(world)).status is OPEN


async def test_the_late_resolution_reads_the_successes_once_per_attempt() -> None:
    world = World()
    world.add_failed_job(world.run, 12)
    reads: list[tuple[Any, ...]] = []
    original = world.store.later_successes

    async def spy(*args: Any) -> Any:
        reads.append(args)
        return await original(*args)

    world.store.later_successes = spy  # type: ignore[method-assign,assignment]

    await world.run_once()

    assert len(reads) == 1


@pytest.mark.parametrize("success_first", [True, False])
async def test_the_final_state_does_not_depend_on_the_order(success_first: bool) -> None:
    world = World()
    success = success_run()

    if success_first:
        await record_success(world, success)
        await world.run_once()
    else:
        await world.run_once()
        await record_success(world, success)

    failure = await failure_of(world)
    assert (failure.status, failure.fix_commit_sha) == (FIX, B)
    resolved = await world.store.resolved_by(world.run.repo_id, 5001, 1)
    assert [f.id for f in resolved] == [failure.id]
