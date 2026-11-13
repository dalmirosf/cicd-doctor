"""Shared pytest configuration."""

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from tests.pg import database_url_for_tests, ensure_database, migrate_to_head, reset_tables

_TESTS_DIR = Path(__file__).parent
_MARKED_DIRECTORIES = {"unit", "contract", "integration", "e2e"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test with the marker named after its top-level directory under ``tests/``.

    This keeps ``pytest -m "unit or contract"`` (the fast pre-commit suite, SPEC D-29) in sync
    with the directory layout, without relying on each file remembering to add a marker.

    A test that is already marked ``integration`` (the Postgres parameter of a contract suite)
    keeps only that marker, so the fast suite never needs a database (D-51).
    """
    for item in items:
        relative = item.path.relative_to(_TESTS_DIR)
        top_level = relative.parts[0]
        if top_level not in _MARKED_DIRECTORIES:
            continue
        if top_level != "integration" and item.get_closest_marker("integration"):
            continue
        item.add_marker(getattr(pytest.mark, top_level))


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """psycopg's async mode needs a selector event loop; Windows defaults to Proactor (D-52).

    One factory only, so pytest-asyncio does not add a parameter id to the tests.
    """
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(scope="session")
def pg_url() -> str:
    """A migrated test database (skipped locally when Postgres is down, D-51)."""
    url = database_url_for_tests()
    ensure_database(url)
    migrate_to_head(url)
    return url


@pytest.fixture
def pg_clean(pg_url: str) -> str:
    """``pg_url`` with empty tables."""
    reset_tables(pg_url)
    return pg_url
