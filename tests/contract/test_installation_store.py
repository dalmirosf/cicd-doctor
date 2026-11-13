"""Contract suite for the ``InstallationStore`` port (FR-03, D-54).

The ``postgres`` parameter is marked ``integration`` (D-51), so the fast suite runs only the
in-memory adapter.
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from cicd_doctor.adapters.memory import InMemoryInstallationStore
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.core.models import Installation, Repository
from cicd_doctor.core.ports import InstallationStore
from tests.factories import T0

INSTALLATION = Installation(id=10, account_login="acme", account_type="Organization")
WIDGETS = Repository(id=100, installation_id=10, full_name="acme/widgets", private=True)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[InstallationStore]:
    if request.param == "memory":
        yield InMemoryInstallationStore()
        return
    url: str = request.getfixturevalue("pg_clean")
    async with PostgresInstallationStore.connect(url) as postgres:
        yield postgres


async def test_unknown_ids_return_none(store: InstallationStore) -> None:
    assert await store.installation(1) is None
    assert await store.repository(1) is None


async def test_an_installation_round_trips_and_is_replaced_on_save(
    store: InstallationStore,
) -> None:
    await store.save_installation(INSTALLATION)
    assert await store.installation(10) == INSTALLATION

    changed = INSTALLATION.model_copy(
        update={"account_login": "acme-inc", "suspended_at": T0, "deleted_at": T0}
    )
    await store.save_installation(changed)

    assert await store.installation(10) == changed


async def test_repositories_round_trip(store: InstallationStore) -> None:
    await store.save_installation(INSTALLATION)
    gadgets = Repository(
        id=101, installation_id=10, full_name="acme/gadgets", private=False, default_branch="main"
    )

    await store.save_repositories([WIDGETS, gadgets])

    assert await store.repository(100) == WIDGETS
    assert await store.repository(101) == gadgets


async def test_saving_without_a_default_branch_keeps_the_known_one(
    store: InstallationStore,
) -> None:
    await store.save_installation(INSTALLATION)
    await store.save_repositories([WIDGETS.model_copy(update={"default_branch": "main"})])

    await store.save_repositories([WIDGETS.model_copy(update={"full_name": "acme/widgets-2"})])

    stored = await store.repository(100)
    assert stored is not None
    assert (stored.full_name, stored.default_branch) == ("acme/widgets-2", "main")


async def test_remove_marks_only_the_given_repositories(store: InstallationStore) -> None:
    await store.save_installation(INSTALLATION)
    gadgets = WIDGETS.model_copy(update={"id": 101, "full_name": "acme/gadgets"})
    await store.save_repositories([WIDGETS, gadgets])
    removed_at = T0 + timedelta(hours=1)

    await store.remove_repositories([100, 999], removed_at)  # 999 is unknown: ignored

    removed, kept = await store.repository(100), await store.repository(101)
    assert removed is not None
    assert removed.removed_at == removed_at
    assert kept == gadgets


async def test_saving_a_repository_again_clears_removed_at(store: InstallationStore) -> None:
    await store.save_installation(INSTALLATION)
    await store.save_repositories([WIDGETS])
    await store.remove_repositories([100], T0)

    await store.save_repositories([WIDGETS])

    assert await store.repository(100) == WIDGETS


async def test_empty_batches_are_no_ops(store: InstallationStore) -> None:
    await store.save_repositories([])
    await store.remove_repositories([], T0)

    assert await store.repository(100) is None
