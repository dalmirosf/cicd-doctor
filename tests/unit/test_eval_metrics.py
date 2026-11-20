"""The P4 (b) metrics (SPEC §11.2, D-109), each against results built by hand.

Nothing here calls a model: the answers are ``CaseResult`` values or a ``FakeLLM``.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter, FakeLLM
from cicd_doctor.adapters.memory.llm import fabricated_evidence, valid
from cicd_doctor.core.diagnosis.cost import CostModel
from evals.runner.metrics import SCHEMA_VERSION, bootstrap_intervals, bucket, compute
from evals.runner.recording import RecordingLLM
from evals.runner.report import Report, render_markdown, write_report
from evals.runner.run import LLMSetup, run_evals_async
from evals.runner.variants import CaseResult
from tests.evalcases import SYNTHETIC, write_case


def result(expected: str, predicted: str, confidence: float = 0.9, **extra: Any) -> CaseResult:
    fields: dict[str, object] = {
        "case_id": f"{expected}-{predicted}-{confidence}-{len(extra)}",
        "source": "lab",
        "variant": "llm",
        "expected": expected,
        "predicted": predicted,
        "confidence": confidence,
    } | extra
    return CaseResult(**fields)  # type: ignore[arg-type]


def answered(
    expected: str,
    predicted: str,
    raw: int,
    valid_items: int,
    latency: int = 100,
    **extra: Any,
) -> CaseResult:
    return result(
        expected,
        predicted,
        diagnosis_source="llm",
        answer_raw_items=raw,
        answer_valid_items=valid_items,
        llm_latency_ms=latency,
        **extra,
    )


# --- latency of the call, and cost per LLM diagnosis ------


def test_model_latency_comes_from_the_recorded_answers_not_from_the_pipeline() -> None:
    results = [
        answered("dependency", "dependency", 1, 1, latency=100, latency_ms=0.5),
        answered("dependency", "dependency", 1, 1, latency=300, latency_ms=0.7),
        answered("dependency", "dependency", 1, 1, latency=900, latency_ms=0.9),
        result("flaky", "flaky", diagnosis_source="cache", latency_ms=0.2),  # no call
    ]

    m = compute(results)

    assert (m.llm_latency_ms_p50, m.llm_latency_ms_p95) == (300, 900)
    assert m.latency_ms_p95 == 0.9  # the pipeline time of this run stays what it was


def test_no_recorded_answer_means_no_model_latency_and_no_llm_cost() -> None:
    m = compute([result("dependency", "dependency")])

    assert m.llm_latency_ms_p50 is None
    assert (m.llm_diagnoses, m.cost_usd_per_llm_diagnosis, m.cost_usd_p95) == (0, None, None)


def test_cost_per_diagnosis_counts_only_the_ones_the_model_made() -> None:
    results = [
        answered("dependency", "dependency", 1, 1, cost_usd=0.002),
        answered("dependency", "dependency", 1, 1, cost_usd=0.004),
        # a fallback that spent money, and a cache hit that did not: neither is an LLM diagnosis
        result("flaky", "flaky", diagnosis_source="rules", cost_usd=0.010, fallback_reason="x"),
        result("flaky", "flaky", diagnosis_source="cache", cost_usd=0.0),
    ]

    m = compute(results, priced=True)

    assert m.llm_diagnoses == 2
    assert m.cost_usd_per_llm_diagnosis == 0.003
    assert m.cost_usd_p95 == 0.004
    assert m.cost_usd_total == 0.016  # the money spent is all of it
    assert m.priced is True


# --- evidence ------


def test_evidence_validity_is_valid_raw_items_over_raw_items() -> None:
    results = [
        answered("dependency", "dependency", 4, 3),
        answered("dependency", "dependency", 2, 0),  # the model quoted lines that do not exist
        answered("flaky", "flaky", 0, 0),  # an answer with no evidence at all
        result("flaky", "flaky", diagnosis_source="cache"),  # no answer: not counted
    ]

    m = compute(results)

    assert m.evidence_validity == 0.5  # 3 of 6
    assert m.answers == 3
    assert m.answers_without_valid_evidence == 2


def test_evidence_validity_without_raw_items_is_unknown_not_zero() -> None:
    assert compute([answered("flaky", "flaky", 0, 0)]).evidence_validity is None
    assert compute([]).evidence_validity is None


# --- calibration and abstention ------


def test_the_confidence_borders_fall_in_the_bucket_above() -> None:
    assert [bucket(c) for c in (0.75, 0.7499, 0.45, 0.4499)] == ["high", "medium", "medium", "low"]

    results = [
        result("dependency", "dependency", 0.75),  # high, right
        result("dependency", "flaky", 0.7499),  # medium, wrong
        result("dependency", "dependency", 0.45),  # medium, right
        result("dependency", "flaky", 0.4499),  # low, wrong
    ]
    m = compute(results)

    assert m.calibration["high"] == {"cases": 1, "accuracy": 1.0}
    assert m.calibration["medium"] == {"cases": 2, "accuracy": 0.5}
    assert m.calibration["low"] == {"cases": 1, "accuracy": 0.0}
    assert m.high_confidence_accuracy == 1.0


def test_deciles_show_only_bins_with_five_cases_and_ece_uses_all_of_them() -> None:
    results = [result("dependency", "dependency", 0.95) for _ in range(4)]  # 0.9 decile, right
    results += [result("dependency", "dependency", 1.0)]  # 1.0 lands in the last decile
    results += [result("dependency", "flaky", 0.45) for _ in range(5)]  # 0.4 decile, wrong
    results += [result("dependency", "dependency", 0.2)]  # a lone case: not shown

    m = compute(results)

    assert [(d["from"], d["to"], d["cases"]) for d in m.calibration_deciles] == [
        (0.4, 0.5, 5),
        (0.9, 1.0, 5),
    ]
    assert m.calibration_deciles[0]["accuracy"] == 0.0
    assert m.calibration_deciles[1]["accuracy"] == 1.0
    # |accuracy - confidence| of each bin, weighted by its cases
    expected = (5 * 0.45 + 5 * 0.04 + 1 * 0.8) / 11
    assert m.expected_calibration_error == pytest.approx(expected, abs=1e-3)


def test_errors_are_not_calibrated_and_nothing_gives_no_ece() -> None:
    boom = result("dependency", "error", 0.0, error="no failed job")

    assert compute([boom]).expected_calibration_error is None
    assert compute([boom]).calibration_deciles == []
    assert compute([]).high_confidence_accuracy is None


def test_saying_unknown_is_not_the_same_as_low_confidence() -> None:
    results = [
        result("unknown", "unknown", 0.9),  # says unknown, sure of it
        result("dependency", "dependency", 0.3),  # an answer, low confidence
        result("flaky", "unknown", 0.1),  # both
        result("flaky", "flaky", 0.8),
    ]

    m = compute(results)

    assert m.abstention_unknown == 0.5
    assert m.abstention_low_confidence == 0.5
    assert m.abstention == 0.75  # the old metric: either


# --- the bootstrap ------


def _mixed() -> list[CaseResult]:
    out = [result("dependency", "dependency") for _ in range(8)]
    out += [result("dependency", "flaky") for _ in range(2)]
    out += [result("flaky", "flaky", source="public") for _ in range(3)]
    out += [result("flaky", "dependency", source="public")]
    out += [result("dependency", "dependency", source="injection", attack=True)]
    return out


def test_the_bootstrap_is_deterministic_and_per_source() -> None:
    first = bootstrap_intervals(_mixed())

    assert first == bootstrap_intervals(_mixed())
    assert set(first) == {"lab", "public", "injection", "lab+public"}
    assert first["lab"]["cases"] == 10
    assert first["lab+public"]["cases"] == 14
    assert first["lab"]["resamples"] == 1000


def test_the_interval_brackets_the_estimate_and_is_a_point_for_a_perfect_group() -> None:
    m = compute(_mixed())
    accuracy = m.intervals["lab"]["accuracy"]
    assert isinstance(accuracy, dict)

    assert accuracy["low"] <= 0.8 <= accuracy["high"]
    assert m.intervals["injection"]["accuracy"] == {"low": 1.0, "high": 1.0}


def test_the_bootstrap_of_nothing_or_one_category() -> None:
    assert bootstrap_intervals([]) == {}
    only = bootstrap_intervals([result("dependency", "dependency") for _ in range(5)])
    assert only["lab"]["macro_f1"] == {"low": 1.0, "high": 1.0}


def test_a_metric_with_zero_cases_does_not_divide_by_zero() -> None:
    m = compute([])

    assert (m.llm_diagnoses, m.answers, m.intervals, m.calibration_deciles) == (0, 0, {}, [])
    assert (m.abstention_unknown, m.abstention_low_confidence) == (0.0, 0.0)


# --- from the runner: the recorder gives the raw answer ------


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    for fixture in SYNTHETIC:
        write_case(root, fixture)
    return root


PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))


async def _run(cases: Path, fake: FakeLLM, cost: CostModel = PRICES) -> Report:
    recorder = RecordingLLM(fake)
    setup = LLMSetup(client=recorder, label="m", cost=cost, recorder=recorder)
    return await run_evals_async(
        "llm", ["lab"], cases, CharTokenCounter(), llms=[setup], now=lambda: datetime.now(UTC)
    )


async def test_the_raw_evidence_counts_what_the_pipeline_dropped(cases_dir: Path) -> None:
    report = await _run(cases_dir, FakeLLM([fabricated_evidence()] * len(SYNTHETIC) * 3))

    run = report.runs[0]
    answered_cases = [r for r in run.results if r.llm_latency_ms is not None]
    assert answered_cases
    assert all(r.answer_raw_items == 2 and r.answer_valid_items == 0 for r in answered_cases)
    assert all(r.llm_latency_ms == 40 for r in answered_cases)  # the FakeLLM's recorded latency
    assert run.metrics.evidence_validity == 0.0
    assert run.metrics.answers_without_valid_evidence == len(answered_cases)


async def test_valid_answers_have_valid_evidence_and_the_report_has_the_new_columns(
    cases_dir: Path, tmp_path: Path
) -> None:
    report = await _run(cases_dir, FakeLLM([valid()] * len(SYNTHETIC) * 3))

    run = report.runs[0]
    assert run.metrics.evidence_validity == 1.0
    assert run.metrics.priced is True
    assert (run.metrics.cost_usd_per_llm_diagnosis or 0) > 0
    json_path, md_path = write_report(report, tmp_path / "reports")
    data = json.loads(json_path.read_text("utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION == 2
    assert data["flaky_sequences"]["accuracy"] == 1.0
    metrics = data["variants"][run.variant]["metrics"]
    assert {"latency_ms_p50", "llm_latency_ms_p50", "evidence_validity", "intervals"} <= set(
        metrics
    )
    markdown = md_path.read_text("utf-8")
    for heading in ("Model call p50", "Evidence validity", "ECE", "bootstrap", "D-107"):
        assert heading in markdown
    assert "with stored answers" in markdown


async def test_without_prices_the_report_says_the_cost_is_zero(cases_dir: Path) -> None:
    report = await _run(cases_dir, FakeLLM([valid()] * len(SYNTHETIC) * 3), CostModel())

    assert report.runs[0].metrics.priced is False
    assert "no prices: cost is 0" in render_markdown(report)
