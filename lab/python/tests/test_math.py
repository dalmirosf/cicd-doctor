"""Case py-assert: the expected value is wrong."""

from labpkg.mathops import add


def test_add() -> None:
    assert add(1, 2) == 4


def test_add_zero() -> None:
    assert add(0, 0) == 0
