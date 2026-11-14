"""Case flaky-timing: a timing assertion that is too tight for a shared runner."""

import random
import time


def test_fast_enough() -> None:
    started = time.perf_counter()
    time.sleep(random.uniform(0.0, 0.1))
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, f"took {elapsed:.3f}s, budget 0.050s"
