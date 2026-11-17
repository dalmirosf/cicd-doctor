"""The ``llm+hints+similar`` variant over an in-memory history (SPEC §11.2, D-86).

FakeLLM and FakeEmbedder only: no network, no model, no database.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter, FakeEmbedder, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis.cost import CostModel
from evals.runner.cases import load_cases
from evals.runner.history import history_order
from evals.runner.report import Report, memory_effects, render_markdown
from evals.runner.run import NEEDS_A_MODEL, EvalError, LLMSetup, run_evals_async
from evals.runner.variants import CaseResult
from tests.evalcases import write_case

COUNTER = CharTokenCounter()
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
SIMILAR = "llm+hints+similar [m]"
T0 = "2026-11-14T01:00:00+00:00"
T1 = "2026-11-14T01:00:10+00:00"


def setup(fake: FakeLLM) -> LLMSetup:
    return LLMSetup(client=fake, label="m", cost=PRICES)


async def run(variant: str, cases: Path, fake: FakeLLM, sources: str = "lab") -> Report:
    return await run_evals_async(
        variant, [sources], cases, COUNTER, llms=[setup(fake)], embedder=FakeEmbedder()
    )


def by_id(report: Report, name: str = SIMILAR) -> dict[str, CaseResult]:
    [run_] = [r for r in report.runs if r.variant == name]
    return {r.case_id: r for r in run_.results}


def edit_log(root: Path, old: str, new: str) -> None:
    log = root / "log.txt"
    log.write_text(log.read_text("utf-8").replace(old, new), encoding="utf-8", newline="")


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    return tmp_path / "cases"


def modnf(cases_dir: Path, case_id: str, run_id: int, captured_at: str | None = T0) -> Path:
    return write_case(
        cases_dir, "py-modnf", case_id=case_id, run_id=run_id, captured_at=captured_at
    )


async def test_a_repeated_failure_is_served_from_the_cache_the_second_time(
    cases_dir: Path,
) -> None:
    modnf(cases_dir, "lab-py-modnf-01", 1, T0)
    modnf(cases_dir, "lab-py-modnf-02", 2, T1)
    fake = FakeLLM([valid()] * 4)

    report = await run("llm+hints+similar", cases_dir, fake)

    results = by_id(report)
    assert results["lab-py-modnf-01"].diagnosis_source == "llm"
    assert results["lab-py-modnf-02"].diagnosis_source == "cache"
    assert results["lab-py-modnf-02"].cost_usd == 0
    assert len(fake.calls) == 1  # the second case reserved nothing and called nothing
    [run_] = report.runs
    assert (run_.metrics.cache_hits, run_.metrics.cache_hit_rate) == (1, 0.5)


async def test_a_different_failure_of_the_same_category_lists_the_earlier_one_as_similar(
    cases_dir: Path,
) -> None:
    modnf(cases_dir, "lab-a", 1, T0)
    other = modnf(cases_dir, "lab-b", 2, T1)
    edit_log(other, "requests", "numpy")  # another signature, almost the same text
    fake = FakeLLM([valid()] * 2)

    report = await run("llm+hints+similar", cases_dir, fake)

    first, second = by_id(report)["lab-a"], by_id(report)["lab-b"]
    assert (first.similar_found, second.diagnosis_source) == (0, "llm")
    assert second.similar_found == 1
    assert second.similar_top_score == pytest.approx(0.964, abs=1e-3)
    [similar] = fake.calls[1].similar
    assert (similar.run_id, similar.score) == (1, second.similar_top_score)
    [run_] = report.runs
    assert (run_.metrics.similar_hits, run_.metrics.cache_hits) == (1, 0)


async def test_the_history_follows_captured_at_not_the_case_id(cases_dir: Path) -> None:
    # ``lab-a`` sorts first by id but was captured last: it is the one that finds the other.
    modnf(cases_dir, "lab-a", 1, "2026-11-14T02:00:00+00:00")
    modnf(cases_dir, "lab-b", 2, T0)
    write_case(cases_dir, "docker-copy", case_id="lab-c", run_id=3)  # no date: last
    assert [c.id for c in history_order(load_cases(cases_dir, ["lab"]))] == [
        "lab-b",
        "lab-a",
        "lab-c",
    ]

    report = await run("llm+hints+similar", cases_dir, FakeLLM([valid()] * 3))

    results = by_id(report)
    assert results["lab-b"].diagnosis_source == "llm"
    assert results["lab-a"].diagnosis_source == "cache"
    assert [r.case_id for r in report.runs[0].results] == ["lab-a", "lab-b", "lab-c"]  # id order


async def test_no_case_sees_itself_or_its_own_run(cases_dir: Path) -> None:
    modnf(cases_dir, "lab-a", 7, T0)
    # The same run (a second job of it): production never lists the failures of the same run.
    other = modnf(cases_dir, "lab-b", 7, T1)
    edit_log(other, "requests", "numpy")
    fake = FakeLLM([valid()] * 2)

    report = await run("llm+hints+similar", cases_dir, fake)

    assert all(r.similar_found == 0 for r in report.runs[0].results)
    assert all(call.similar == [] for call in fake.calls)


async def test_a_cached_answer_expires_with_the_clock_of_the_history(cases_dir: Path) -> None:
    modnf(cases_dir, "lab-a", 1, "2026-11-01T00:00:00+00:00")
    modnf(cases_dir, "lab-b", 2, "2026-11-20T00:00:00+00:00")
    fake = FakeLLM([valid()] * 2)

    report = await run("llm+hints+similar", cases_dir, fake)

    assert by_id(report)["lab-b"].diagnosis_source == "llm"  # 19 days later: past CACHE_TTL_DAYS
    assert len(fake.calls) == 2


async def test_injection_cases_have_no_history(cases_dir: Path) -> None:
    modnf(cases_dir, "lab-a", 1, T0)
    write_case(cases_dir, "py-modnf", case_id="inj-a", run_id=2, source="injection")
    modnf(cases_dir, "lab-b", 3, T1)
    fake = FakeLLM([valid()] * 4)

    report = await run_evals_async(
        "llm+hints+similar",
        ["lab", "injection"],
        cases_dir,
        COUNTER,
        llms=[setup(fake)],
        embedder=FakeEmbedder(),
    )

    results = by_id(report)
    assert results["inj-a"].diagnosis_source == "llm"  # it did not get the cache...
    assert results["inj-a"].similar_found == 0
    assert results["lab-b"].diagnosis_source == "cache"  # ...and what it said was not kept
    assert len(fake.calls) == 2  # lab-a and the attack
    [run_] = report.runs
    assert run_.metrics.cache_hits == 1
    assert run_.metrics.cache_hit_rate == 0.5  # over the two lab cases: the attack is not counted


async def test_the_report_has_the_new_column_and_the_comparison_with_llm_plus_hints(
    cases_dir: Path,
) -> None:
    modnf(cases_dir, "lab-a", 1, T0)
    modnf(cases_dir, "lab-b", 2, T1)
    fake = FakeLLM([valid()] * 5)  # llm: 2, llm+hints: 2, llm+hints+similar: 1

    report = await run("all", cases_dir, fake)
    markdown = render_markdown(report)

    assert "| llm+hints+similar [m] |" in markdown
    assert "Cache hits" in markdown
    assert "Similar hits" in markdown
    [effect] = memory_effects(report)
    assert effect.model == "m"
    assert effect.cache_hits == 1
    assert effect.cost_delta_usd < 0  # one call less
    assert "Small n" in markdown
    assert "py-modnf" in markdown
    assert "1 (50.0%)" in markdown
    assert report.to_dict()["memory_effect"]


async def test_without_the_variant_there_is_no_comparison(cases_dir: Path) -> None:
    modnf(cases_dir, "lab-a", 1)

    report = await run("llm+hints", cases_dir, FakeLLM([valid()]))

    assert memory_effects(report) == []
    assert "Effect of the memory" not in render_markdown(report)


async def test_without_a_model_the_variant_is_skipped_with_the_standard_text(
    cases_dir: Path,
) -> None:
    modnf(cases_dir, "lab-a", 1)

    report = await run_evals_async("all", ["lab"], cases_dir, COUNTER)

    assert report.skipped["llm+hints+similar"] == NEEDS_A_MODEL
    with pytest.raises(EvalError, match="needs a model"):
        await run_evals_async("llm+hints+similar", ["lab"], cases_dir, COUNTER)


async def test_the_variant_refuses_to_run_without_an_embedder(cases_dir: Path) -> None:
    modnf(cases_dir, "lab-a", 1)

    with pytest.raises(EvalError, match="needs an embedder"):
        await run_evals_async(
            "llm+hints+similar", ["lab"], cases_dir, COUNTER, llms=[setup(FakeLLM([valid()]))]
        )
