"""The daily purge (FR-18, D-34, D-85) on memory adapters and a ``FakeClock``."""

import logging
from datetime import timedelta
from uuid import uuid4

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    FakeReporter,
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
)
from cicd_doctor.core.models import (
    CommentTarget,
    Delivery,
    FailureStatus,
    Installation,
    InstallationAction,
    InstallationEvent,
    JobKind,
    NewJob,
    Publication,
    PublicationKey,
    PurgeCounts,
    Repository,
)
from cicd_doctor.core.usecases.purge import PurgeConfig, run_purge
from cicd_doctor.core.usecases.track_installation import track_installation
from tests.factories import T0, make_run_analysis, make_run_ref
from tests.factories import make_workflow_run as workflow_run

DAY = timedelta(days=1)
CONFIG = PurgeConfig()


class World:
    """Everything the purge touches, on one fake clock."""

    def __init__(self) -> None:
        self.clock = FakeClock(T0)
        self.queue = InMemoryJobQueue(self.clock)
        self.store = InMemoryFailureStore(self.clock)
        self.installations = InMemoryInstallationStore()

    async def purge(self, config: PurgeConfig = CONFIG) -> PurgeCounts:
        return await run_purge(self.queue, self.store, self.installations, self.clock, config)

    async def delivery(self) -> str:
        delivery = Delivery(
            delivery_id=uuid4(), event="workflow_run", action="completed", installation_id=1
        )
        await self.queue.enqueue(NewJob(kind=JobKind.DIAGNOSE_RUN, payload={}), delivery)
        return str(delivery.delivery_id)

    async def analyse(self, run_id: int, age_days: int, repo_id: int = 100) -> None:
        run = make_run_ref(run_id=run_id, repo_id=repo_id)
        record = make_run_analysis(run)
        attempt = record.attempt.model_copy(
            update={"completed_at": self.clock.now() - age_days * DAY}
        )
        await self.store.save_run_analysis(record.model_copy(update={"attempt": attempt}))

    async def status(self, run_id: int) -> FailureStatus:
        record = await self.store.run_analysis(run_id, 1)
        assert record is not None
        return record.items[0].failure.status


@pytest.fixture
def world() -> World:
    return World()


async def test_deliveries_of_31_days_are_deleted_and_those_of_29_are_not(world: World) -> None:
    old = await world.delivery()
    world.clock.advance(2 * DAY)
    recent = await world.delivery()
    world.clock.advance(29 * DAY)

    counts = await world.purge()

    assert counts.deleted["webhook_deliveries"] == 1
    assert old not in world.queue.deliveries
    assert recent in world.queue.deliveries


async def test_failures_of_91_days_go_with_their_diagnoses_and_those_of_89_stay(
    world: World,
) -> None:
    await world.analyse(5000, 91)
    await world.analyse(5001, 89)
    old_ids = [item.failure.id for item in (await world.store.run_analysis(5000, 1)).items]  # type: ignore[union-attr]
    assert all(world.store.diagnosis_rows(failure_id) for failure_id in old_ids)

    counts = await world.purge()

    assert counts.deleted["failures"] == 3
    assert await world.store.run_analysis(5000, 1) is None
    assert await world.store.run_analysis(5001, 1) is not None
    assert not any(world.store.diagnosis_rows(failure_id) for failure_id in old_ids)


async def test_open_failures_turn_stale_at_31_days_and_not_at_29(world: World) -> None:
    await world.analyse(5000, 0)
    world.clock.advance(2 * DAY)
    await world.analyse(5001, 0)
    world.clock.advance(29 * DAY)  # 31 and 29 days since they were stored

    await world.purge()

    assert await world.status(5000) is FailureStatus.STALE
    assert await world.status(5001) is FailureStatus.OPEN


async def test_a_resolved_failure_of_40_days_does_not_turn_stale(world: World) -> None:
    await world.analyse(5000, 0)
    record = await world.store.run_analysis(5000, 1)
    assert record is not None
    await world.store.resolve(
        [item.failure.id for item in record.items], FailureStatus.RESOLVED_BY_COMMIT, "b" * 40
    )
    world.clock.advance(40 * DAY)

    await world.purge()

    assert await world.status(5000) is FailureStatus.RESOLVED_BY_COMMIT


async def test_finished_jobs_of_31_days_are_deleted_and_live_ones_stay(world: World) -> None:
    await world.queue.enqueue(NewJob(kind=JobKind.EXPLAIN, payload={}, dedupe_key="done"), None)
    job = await world.queue.lease("w", timedelta(seconds=300))
    assert job is not None
    await world.queue.complete(job)
    await world.queue.enqueue(NewJob(kind=JobKind.EXPLAIN, payload={}, dedupe_key="live"), None)
    world.clock.advance(31 * DAY)

    counts = await world.purge()

    assert counts.deleted["jobs"] == 1
    assert await world.queue.get(job.id) is None
    assert await world.queue.get(2) is not None


async def _two_installations(world: World) -> None:
    for installation_id, login, repo_id in ((10, "acme", 100), (11, "neighbour", 200)):
        installation = Installation(id=installation_id, account_login=login, account_type="User")
        await world.installations.save_installation(installation)
        await world.installations.save_repositories(
            [
                Repository(
                    id=repo_id,
                    installation_id=installation_id,
                    full_name=f"{login}/widgets",
                    private=False,
                )
            ]
        )
        await world.analyse(repo_id * 10, 1, repo_id=repo_id)


def uninstall(at_days_ago: int, world: World) -> InstallationEvent:
    return InstallationEvent(
        action=InstallationAction.DELETED,
        installation=Installation(id=10, account_login="acme", account_type="User"),
        occurred_at=world.clock.now() - at_days_ago * DAY,
    )


async def test_an_uninstalled_installation_is_purged_after_the_grace_and_not_before(
    world: World,
) -> None:
    await _two_installations(world)
    await track_installation(uninstall(6, world), world.installations)

    assert (await world.purge()).total == 0
    assert await world.store.run_analysis(1000, 1) is not None

    world.clock.advance(2 * DAY)  # 8 days since the uninstall
    counts = await world.purge()

    assert counts.deleted["workflow_runs"] == 1
    assert await world.store.run_analysis(1000, 1) is None
    assert await world.store.run_analysis(2000, 1) is not None  # the neighbour is never touched
    assert await world.installations.repository(100) is not None  # soft delete: the row stays


async def test_nothing_is_deleted_when_the_installation_comes_back_within_the_grace(
    world: World,
) -> None:
    await _two_installations(world)
    await track_installation(uninstall(6, world), world.installations)
    await track_installation(
        InstallationEvent(
            action=InstallationAction.CREATED,
            installation=Installation(id=10, account_login="acme", account_type="User"),
            occurred_at=world.clock.now(),
            repositories=[
                Repository(id=100, installation_id=10, full_name="acme/widgets", private=False)
            ],
        ),
        world.installations,
    )
    world.clock.advance(30 * DAY)  # long after the grace of the first uninstall

    counts = await world.purge()

    assert counts.deleted.get("workflow_runs", 0) == 0
    assert await world.store.run_analysis(1000, 1) is not None


async def test_a_removed_repository_is_purged_after_the_grace_and_its_neighbour_is_not(
    world: World,
) -> None:
    await _two_installations(world)
    await world.installations.remove_repositories([100], world.clock.now() - 8 * DAY)

    counts = await world.purge()

    assert counts.deleted["workflow_runs"] == 1
    assert await world.store.run_analysis(1000, 1) is None
    assert await world.store.run_analysis(2000, 1) is not None


async def test_an_old_comment_publication_is_deleted_and_a_new_comment_finds_its_own(
    world: World,
) -> None:
    run = make_run_ref()
    await world.store.save_run(workflow_run(run), make_run_analysis(run).attempt)
    key = PublicationKey.comment(repo_id=run.repo_id, pr_number=12, workflow_id=run.workflow_id)
    reporter = FakeReporter()
    target = CommentTarget(run=run, pr_number=12)
    body = "<!-- cicd-doctor:comment:7 -->\nfirst"
    comment_id = await reporter.upsert_comment(target, body, None)
    await world.store.save_publication(
        Publication(key=key, external_id=comment_id, run_id=run.run_id, attempt=1)
    )
    world.clock.advance(91 * DAY)

    counts = await world.purge()
    assert counts.deleted["publications"] == 1
    assert await world.store.publication(key) is None

    # the reporter has no id hint any more and finds the comment by its marker: no duplicate
    again = await reporter.upsert_comment(target, body.replace("first", "second"), None)
    assert again == comment_id
    assert len(reporter.comments) == 1


async def test_a_second_run_right_after_deletes_nothing(world: World) -> None:
    await world.delivery()
    await world.analyse(5000, 91)
    await world.analyse(5001, 0)
    await _two_installations(world)
    await world.installations.remove_repositories([100], world.clock.now() - 8 * DAY)
    world.clock.advance(31 * DAY)
    first = await world.purge()
    assert first.total > 0

    second = await world.purge()

    assert second.total == 0


async def test_the_retention_windows_come_from_the_config(world: World) -> None:
    await world.analyse(5000, 11)
    await world.analyse(5001, 9)

    await world.purge(PurgeConfig(failures_days=10))

    assert await world.store.run_analysis(5000, 1) is None
    assert await world.store.run_analysis(5001, 1) is not None


async def test_the_log_carries_counts_and_no_content(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    await world.analyse(5000, 91)
    caplog.set_level(logging.INFO)

    await world.purge()

    message = next(r.getMessage() for r in caplog.records if "purge done" in r.getMessage())
    assert "failures=3" in message
    assert "acme" not in message
    assert "ModuleNotFoundError" not in message
