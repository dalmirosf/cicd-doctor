"""Case flaky-random: fails about half of the time, with no code change."""

import random


def test_coin_flip() -> None:
    assert random.random() < 0.5, "the coin landed on the wrong side"
