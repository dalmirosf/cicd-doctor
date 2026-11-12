"""Shared pytest configuration."""

from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_MARKED_DIRECTORIES = {"unit", "contract", "integration", "e2e"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test with the marker named after its top-level directory under ``tests/``.

    This keeps ``pytest -m "unit or contract"`` (the fast pre-commit suite, SPEC D-29) in sync
    with the directory layout, without relying on each file remembering to add a marker.
    """
    for item in items:
        relative = item.path.relative_to(_TESTS_DIR)
        top_level = relative.parts[0]
        if top_level in _MARKED_DIRECTORIES:
            item.add_marker(getattr(pytest.mark, top_level))
