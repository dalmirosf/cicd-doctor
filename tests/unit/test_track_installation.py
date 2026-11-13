from datetime import timedelta

from cicd_doctor.adapters.memory import InMemoryInstallationStore
from cicd_doctor.core.models import (
    Installation,
    InstallationAction,
    InstallationEvent,
    Repository,
)
from cicd_doctor.core.usecases.track_installation import track_installation
from tests.factories import T0

INSTALLATION = Installation(id=10, account_login="acme", account_type="Organization")
WIDGETS = Repository(id=100, installation_id=10, full_name="acme/widgets", private=True)
GADGETS = Repository(id=101, installation_id=10, full_name="acme/gadgets", private=False)


def event(action: InstallationAction, **changes: object) -> InstallationEvent:
    fields: dict[str, object] = {
        "action": action,
        "installation": INSTALLATION,
        "occurred_at": T0,
    } | changes
    return InstallationEvent.model_validate(fields)


async def test_created_stores_the_installation_and_its_repositories() -> None:
    store = InMemoryInstallationStore()

    await track_installation(
        event(InstallationAction.CREATED, repositories=[WIDGETS, GADGETS]), store
    )

    assert await store.installation(10) == INSTALLATION
    assert await store.repository(100) == WIDGETS
    assert await store.repository(101) == GADGETS


async def test_created_again_clears_a_previous_deletion_and_suspension() -> None:
    store = InMemoryInstallationStore()
    await store.save_installation(
        INSTALLATION.model_copy(update={"deleted_at": T0, "suspended_at": T0})
    )

    await track_installation(event(InstallationAction.CREATED), store)

    stored = await store.installation(10)
    assert stored is not None
    assert (stored.deleted_at, stored.suspended_at) == (None, None)


async def test_deleted_keeps_the_rows_and_marks_the_installation() -> None:
    store = InMemoryInstallationStore()
    await track_installation(event(InstallationAction.CREATED, repositories=[WIDGETS]), store)
    later = T0 + timedelta(days=1)

    await track_installation(
        event(InstallationAction.DELETED, repositories=[WIDGETS], occurred_at=later), store
    )

    stored = await store.installation(10)
    assert stored is not None
    assert stored.deleted_at == later
    assert await store.repository(100) == WIDGETS


async def test_suspend_and_unsuspend_toggle_suspended_at_and_keep_deleted_at() -> None:
    store = InMemoryInstallationStore()
    await store.save_installation(INSTALLATION)
    later = T0 + timedelta(hours=1)

    await track_installation(event(InstallationAction.SUSPENDED, occurred_at=later), store)
    suspended = await store.installation(10)
    await track_installation(event(InstallationAction.UNSUSPENDED), store)
    unsuspended = await store.installation(10)

    assert suspended is not None
    assert suspended.suspended_at == later
    assert unsuspended is not None
    assert unsuspended.suspended_at is None


async def test_repositories_added_to_an_unknown_installation_create_it() -> None:
    """The app may have been installed before this code tracked installations."""
    store = InMemoryInstallationStore()

    await track_installation(
        event(InstallationAction.REPOSITORIES_ADDED, repositories=[WIDGETS]), store
    )

    assert await store.installation(10) == INSTALLATION
    assert await store.repository(100) == WIDGETS


async def test_removed_repositories_are_marked_and_added_back_later() -> None:
    store = InMemoryInstallationStore()
    await track_installation(event(InstallationAction.CREATED, repositories=[WIDGETS]), store)
    removed_at = T0 + timedelta(hours=2)

    await track_installation(
        event(
            InstallationAction.REPOSITORIES_REMOVED,
            removed_repository_ids=[100],
            occurred_at=removed_at,
        ),
        store,
    )
    removed = await store.repository(100)
    await track_installation(
        event(InstallationAction.REPOSITORIES_ADDED, repositories=[WIDGETS]), store
    )
    restored = await store.repository(100)

    assert removed is not None
    assert removed.removed_at == removed_at
    assert restored is not None
    assert restored.removed_at is None


async def test_account_changes_are_picked_up_without_losing_state() -> None:
    store = InMemoryInstallationStore()
    await store.save_installation(INSTALLATION.model_copy(update={"suspended_at": T0}))
    renamed = INSTALLATION.model_copy(update={"account_login": "acme-inc"})

    await track_installation(
        event(InstallationAction.REPOSITORIES_ADDED, installation=renamed), store
    )

    stored = await store.installation(10)
    assert stored is not None
    assert (stored.account_login, stored.suspended_at) == ("acme-inc", T0)


async def test_the_same_event_twice_gives_the_same_state() -> None:
    store = InMemoryInstallationStore()
    created = event(InstallationAction.CREATED, repositories=[WIDGETS])

    await track_installation(created, store)
    first = (await store.installation(10), await store.repository(100))
    await track_installation(created, store)

    assert (await store.installation(10), await store.repository(100)) == first
