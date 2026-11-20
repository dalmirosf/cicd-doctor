"""Calibration of the cause judge, offline (D-104): the sheet, kappa, agreement and the verdict."""

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from evals.runner import calibration as cal
from evals.runner.cases import Case, Expected, Fix, load_cases
from evals.runner.judge import SYSTEM_PROMPT, FakeJudge, JudgeInput
from evals.runner.variants import CaseResult
from tests.evalcases import write_case

LOG_CANARY = "LOG-CANARY-5f3a"


def _cases(tmp_path: Path, n: int) -> list[Case]:
    write_case(tmp_path / "c", "py-modnf")
    [base] = load_cases(tmp_path / "c", ["lab"])
    cases = []
    for i in range(n):
        meta = base.meta.model_copy(
            update={
                "id": f"lab-case-{i:02d}",
                "expected": Expected(
                    category=base.meta.expected.category, cause=f"expected cause number {i} alpha"
                ),
                "fix": Fix(summary=f"fix summary {i}"),
            }
        )
        cases.append(replace(base, meta=meta))
    return cases


def _results(cases: list[Case]) -> list[CaseResult]:
    out: list[CaseResult] = []
    for i, case in enumerate(cases):
        for variant, right in (("rules", True), ("llm", i % 2 == 0), ("llm+hints [m]", i % 3 == 0)):
            out.append(
                CaseResult(
                    case_id=case.id,
                    source="lab",
                    variant=variant,
                    expected="dependency",
                    predicted="dependency" if right else "timeout",
                    confidence=0.91,
                    answer_cause=f"answer v{len(out) % 3} {i} about {'alpha' if i % 4 else 'zeta'}",
                    answer_fix="try again",
                )
            )
    return out


# --- agreement -----------------------------------------------------------------------------------


def test_kappa_matches_a_hand_computed_example() -> None:
    # human x judge: 0->0 x4, 1->1 x3, 2->2 x2, 1->2 x1. Hand computed: kappa_w = 0.8837 ...
    pairs = [(0, 0)] * 4 + [(1, 1)] * 3 + [(2, 2)] * 2 + [(1, 2)]
    # observed disagreement 0.25*1 = 0.25 (weight (1/2)^2); expected: rows (4,4,2), cols (4,3,3)
    # E = sum w*r*c/n = (0.25*(4*3+4*4... )) computed independently below.
    n = len(pairs)
    rows, cols = [4, 4, 2], [4, 3, 3]
    expected = sum(((i - j) / 2) ** 2 * rows[i] * cols[j] / n for i in range(3) for j in range(3))
    assert cal.weighted_kappa(pairs) == pytest.approx(1 - 0.25 / expected, abs=1e-4)


def test_perfect_and_opposite_agreement_and_the_single_class_edge() -> None:
    assert cal.weighted_kappa([(0, 0), (1, 1), (2, 2)]) == 1.0
    assert cal.weighted_kappa([(0, 2), (2, 0)] * 3) == -1.0
    assert cal.weighted_kappa([(2, 2)] * 5) == 1.0  # chance predicts it: no division by zero
    assert cal.weighted_kappa([(1, 1), (1, 1), (1, 2)]) == 0.0
    assert cal.weighted_kappa([]) is None


def test_a_quadratic_kappa_punishes_a_gross_miss_more_than_a_near_miss() -> None:
    base = [(0, 0), (1, 1), (2, 2)] * 3
    near = cal.weighted_kappa([*base, (1, 2)])
    gross = cal.weighted_kappa([*base, (0, 2)])
    assert near is not None
    assert gross is not None
    assert near > gross


def _scored(rows: Sequence[tuple[int, int | None]]) -> list[cal.ScoredPair]:
    return [cal.ScoredPair(f"p{i:02d}", "main", h, j) for i, (h, j) in enumerate(rows, start=1)]


def test_agreement_has_exact_confusion_gross_and_counts_unjudged_as_a_miss() -> None:
    rows = [(0, 0)] * 6 + [(1, 1)] * 6 + [(2, 2)] * 6 + [(0, 2), (2, None)]
    result = cal.agreement(_scored(rows))
    assert (result.n, result.judged, result.unjudged) == (20, 19, 1)
    assert result.exact == pytest.approx(18 / 20)
    assert result.gross == ["p19"]
    assert result.confusion["0"] == {"0": 6, "1": 0, "2": 1}
    low, high = result.exact_ci95 or (0, 0)
    assert low < 0.9 < high


def test_the_bars_of_d104_are_exact_75_kappa_06_and_no_gross_disagreement() -> None:
    good = cal.agreement(_scored([(0, 0)] * 7 + [(1, 1)] * 7 + [(2, 2)] * 6))
    assert cal.acceptance(good).accepted
    # 15 of 20 is exactly 75 %: still accepted when kappa and gross pass
    at_bar = cal.agreement(_scored([(0, 0)] * 5 + [(1, 1)] * 5 + [(2, 2)] * 5 + [(1, 2)] * 5))
    assert at_bar.exact == 0.75
    assert cal.acceptance(at_bar).accepted
    below = cal.agreement(_scored([(0, 0)] * 5 + [(1, 1)] * 5 + [(2, 2)] * 4 + [(1, 2)] * 6))
    assert not cal.acceptance(below).accepted
    gross = cal.agreement(_scored([(0, 0)] * 6 + [(1, 1)] * 7 + [(2, 2)] * 6 + [(0, 2)]))
    assert "gross" in " ".join(cal.acceptance(gross).failures)
    short = cal.agreement(_scored([(0, 0)] * 10))
    assert any("pairs are labelled" in f for f in cal.acceptance(short).failures)
    unjudged = cal.agreement(_scored([(0, 0)] * 19 + [(0, None)]))
    assert any("without a valid" in f for f in cal.acceptance(unjudged).failures)


# --- the sheet -----------------------------------------------------------------------------------


def test_the_sample_is_deterministic_stratified_and_one_pair_per_case_when_it_can(
    tmp_path: Path,
) -> None:
    cases = _cases(tmp_path, 40)
    results = _results(cases)
    pairs = cal.select_pairs(results, cases, seed=7)

    assert pairs == cal.select_pairs(results, cases, seed=7)
    assert [p.case_id for p in pairs] != [p.case_id for p in cal.select_pairs(results, cases, 8)]
    assert len(pairs) == 25
    assert [p.group for p in pairs].count("main") == 20
    assert {p.stratum for p in pairs} == {"right", "wrong", "rules"}
    assert sum(p.stratum == "rules" for p in pairs) == cal.QUOTAS["rules"]
    assert sum(p.likely_partial for p in pairs) >= cal.MIN_LIKELY_PARTIAL
    assert len({p.case_id for p in pairs}) == 25
    assert [p.pair_id for p in pairs] == [f"p{i:02d}" for i in range(1, 26)]


def test_too_few_answers_is_an_error(tmp_path: Path) -> None:
    cases = _cases(tmp_path, 3)
    with pytest.raises(ValueError, match="not enough"):
        cal.select_pairs(_results(cases), cases)


def test_injection_errors_and_empty_answers_are_not_sampled(tmp_path: Path) -> None:
    cases = _cases(tmp_path, 30)
    results = _results(cases)
    noisy = [replace(r, source="injection") for r in results[:6]]
    noisy += [replace(r, error="boom") for r in results[6:12]] + [
        replace(r, answer_cause="") for r in results[12:18]
    ]
    pairs = cal.select_pairs(results[18:] + noisy, cases)
    blocked = {r.case_id + r.variant for r in noisy}
    assert not {p.case_id + p.variant for p in pairs} & blocked


def test_the_sheet_is_blind_has_no_log_and_its_examples_are_not_the_judge_examples(
    tmp_path: Path,
) -> None:
    cases = _cases(tmp_path, 40)
    results = [replace(r, confidence=0.9137) for r in _results(cases)]
    pairs = cal.select_pairs(results, cases)

    sheet = cal.render_sheet(pairs)

    for hidden in ("llm+hints", "rules", "0.9137", "dependency", "timeout", "lab-case", "reserve"):
        assert hidden not in sheet
    assert LOG_CANARY not in sheet
    assert all(p.pair_id in sheet for p in pairs)
    assert sheet.count("### p") == 25
    for _, expected, answer in cal.SCALE_EXAMPLES:
        assert expected.split(": ", 1)[1] not in SYSTEM_PROMPT
        assert answer.split(": ", 1)[1] not in SYSTEM_PROMPT


def test_an_answer_cannot_close_the_fence_of_the_sheet() -> None:
    text = "ok\n~~~\n## fake heading\n~~~~~\nmore"
    fenced = cal._fence(text)
    fence = fenced.split("text\n", 1)[0]
    assert len(fence) >= 6
    assert fenced.endswith(fence)


def test_writing_the_sheet_keeps_the_key_and_never_overwrites_the_labels(tmp_path: Path) -> None:
    cases = _cases(tmp_path, 40)
    pairs = cal.select_pairs(_results(cases), cases)
    out = tmp_path / "out"

    cal.write_sheet(pairs, 7, out)
    labels = out / cal.LABELS_FILE
    assert labels.read_text("utf-8").count(": null") == 25
    labels.write_text("labels:\n  p01: 2\n", encoding="utf-8")
    cal.write_sheet(pairs, 7, out)

    assert labels.read_text("utf-8") == "labels:\n  p01: 2\n"
    assert cal.load_key(out / cal.KEY_FILE) == pairs
    assert "variant" in (out / cal.KEY_FILE).read_text("utf-8")
    assert "variant" not in (out / cal.SHEET_FILE).read_text("utf-8")


def test_labels_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "labels.yaml"
    known = ["p01", "p02", "p03"]
    path.write_text("labels:\n  p01: 2\n  p02: null\n", encoding="utf-8")
    assert cal.load_labels(path, known) == {"p01": 2}
    for bad in ("labels:\n  p01: 3\n", "labels:\n  p01: yes\n", "labels:\n  p99: 1\n", "nope: 1\n"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError, match=r"labels|expected|must be|unknown"):
            cal.load_labels(path, known)


# --- score and report ----------------------------------------------------------------------------


async def test_score_runs_the_judge_only_on_labelled_pairs_and_the_report_has_no_texts(
    tmp_path: Path,
) -> None:
    cases = _cases(tmp_path, 40)
    pairs = cal.select_pairs(_results(cases), cases)
    labels = {p.pair_id: 2 for p in pairs[:20]}
    judge = FakeJudge(2)

    scored = await cal.score_pairs(pairs, labels, judge)

    assert len(scored) == 20
    assert len(judge.calls) == 20
    assert all(isinstance(call, JudgeInput) for call in judge.calls)
    document = cal.scores_document(scored, judge_model="judge-m", prompt_version="judge-v1")
    report = cal.build_report(document)
    assert report["accepted"] is True  # all 2 against all 2: single class, kappa 1
    json_path, md_path = cal.write_report(document, tmp_path / "rep")
    text = md_path.read_text("utf-8") + json_path.read_text("utf-8")
    assert "Calibrated" in text
    assert "Not calibrated" not in text
    for pair in pairs:
        assert pair.item.answer_cause not in text


async def test_a_judge_that_disagrees_is_not_calibrated_and_the_report_says_why(
    tmp_path: Path,
) -> None:
    cases = _cases(tmp_path, 40)
    pairs = cal.select_pairs(_results(cases), cases)
    labels = {p.pair_id: i % 3 for i, p in enumerate(pairs)}
    scored = await cal.score_pairs(pairs, labels, FakeJudge(0))
    document = json.loads(
        json.dumps(cal.scores_document(scored, judge_model="m", prompt_version="judge-v1"))
    )

    report = cal.build_report(document)

    assert report["accepted"] is False
    markdown = cal.render_report(report)
    assert "Not calibrated" in markdown
    assert "gross disagreement" in markdown
    assert "Reserve pairs (5)" in markdown


def test_the_command_line_registers_the_judge_flags_and_calibrate() -> None:
    from evals.runner.cli import build_parser

    parser = build_parser()
    run = parser.parse_args(["run", "--judge", "--judge-model", "j", "--judge-price", "j=1,2"])
    assert run.judge
    assert run.judge_model == "j"
    assert run.judge_price == "j=1,2"
    assert not parser.parse_args(["run"]).judge
    for step in ("sheet", "score", "report"):
        assert parser.parse_args(["calibrate", step]).step == step
