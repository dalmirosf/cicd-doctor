"""The flaky and fix metric (D-107): sequences of run events judged by an oracle that does not
call the state machine it judges.

Manual mutation check (as in D-99), run by hand and not committed: in ``core/outcomes.py`` change
``if run.run_id == success.run_id:`` to ``if run.run_id != success.run_id:``, or drop the
``run.head_sha == success.head_sha`` test, and ``test_the_oracle_and_the_state_machine_agree``,
``test_every_scenario_is_classified_as_its_label`` and the metric test fail (see the PR report).
``test_a_broken_state_machine_is_caught`` is the same check in the small, with a monkeypatch.
"""

import random
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cicd_doctor.core.models import Failure
from cicd_doctor.core.outcomes import Resolution, decide_late
from evals.runner import flaky
from evals.runner.flaky import (
    BUILDERS,
    FIX,
    FLAKY,
    OPEN,
    ORACLE,
    EventSequence,
    RunEvent,
    classify,
    generate,
    lab_sequences,
    measure,
    run_flaky_metric,
)

SCENARIOS = sorted(BUILDERS)


def test_every_scenario_has_a_hand_written_label() -> None:
    assert set(ORACLE) == set(BUILDERS)
    assert set(ORACLE.values()) == {FLAKY, FIX, OPEN}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_is_classified_as_its_label(scenario: str) -> None:
    for seed in range(20):  # several shas, ids and arrival orders each
        sequence = BUILDERS[scenario](random.Random(seed))
        assert sequence.scenario == scenario
        assert classify(sequence) == ORACLE[scenario], (scenario, seed)


@given(st.integers(min_value=0, max_value=10**6), st.sampled_from(SCENARIOS))
def test_the_oracle_and_the_state_machine_agree(seed: int, scenario: str) -> None:
    sequence = BUILDERS[scenario](random.Random(seed))

    assert classify(sequence) == sequence.expected


def test_the_generator_is_seeded_and_covers_every_scenario() -> None:
    first, again = generate(300, seed=7), generate(300, seed=7)

    assert first == again
    assert first != generate(300, seed=8)
    assert {s.scenario for s in first} == set(ORACLE)


def test_the_metric_is_perfect_over_300_sequences_and_the_two_lab_ones() -> None:
    m = run_flaky_metric()

    assert m.sequences == 302
    assert (m.accuracy, m.misses) == (1.0, [])
    assert (m.flaky_precision, m.flaky_recall) == (1.0, 1.0)
    assert (m.by_commit_precision, m.by_commit_recall) == (1.0, 1.0)
    assert (m.lab_sequences, m.lab_correct) == (2, 2)
    assert all(cell["sequences"] == cell["correct"] for cell in m.by_scenario.values())
    assert [s.scenario for s in lab_sequences()] == ["flaky_rerun", "fix_by_commit"]
    assert all("synthetic" in s.note for s in lab_sequences())


def test_a_broken_state_machine_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine that never resolves by commit must show up in the metric."""
    real = decide_late

    def never_fixes(failure: Failure, successes: Any) -> Resolution | None:
        resolution = real(failure, successes)
        if resolution is not None and resolution.status.value == "resolved_by_commit":
            return None
        return resolution

    monkeypatch.setattr(flaky, "decide_late", never_fixes)

    m = run_flaky_metric()

    assert m.accuracy is not None
    assert m.accuracy < 1.0
    assert (m.by_commit_recall or 0) < 1.0
    assert m.misses


def test_a_machine_that_calls_everything_flaky_loses_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flaky, "classify", lambda sequence: FLAKY)

    m = measure(generate(60, seed=1))

    assert m.flaky_recall == 1.0
    assert (m.flaky_precision or 1) < 0.5
    assert m.by_commit_recall == 0.0


def test_no_sequences_give_no_ratios() -> None:
    m = measure([])

    assert (m.sequences, m.accuracy, m.flaky_precision, m.by_commit_recall) == (0, None, None, None)


def test_a_duplicate_delivery_does_not_change_the_answer() -> None:
    fail = RunEvent("failure", 10, 1, "a" * 40)
    ok = RunEvent("success", 10, 2, "a" * 40)

    assert classify(EventSequence("duplicate_delivery_flaky", (fail, ok, fail, ok))) == FLAKY
