"""Eval runner (SPEC §11, FR-22 skeleton, D-61): cases, the rules variant, metrics, reports."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cicd_doctor.adapters.memory import CharTokenCounter
from evals.runner import cli
from evals.runner.cases import load_cases
from evals.runner.metrics import bucket, compute
from evals.runner.report import render_markdown, write_report
from evals.runner.run import EvalError, resolve_variants, run_evals
from evals.runner.variants import CaseResult, run_rules
from tests.evalcases import SYNTHETIC, write_case

COUNTER = CharTokenCounter()
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    for fixture in SYNTHETIC:
        write_case(tmp_path, fixture)
    return tmp_path


def _result(expected: str, predicted: str, confidence: float = 0.9, **extra: object) -> CaseResult:
    fields: dict[str, object] = {
        "case_id": f"{expected}-{predicted}-{confidence}",
        "source": "lab",
        "variant": "rules",
        "expected": expected,
        "predicted": predicted,
        "confidence": confidence,
    } | extra
    return CaseResult(**fields)  # type: ignore[arg-type]


# --- cases -------------------------------------------------------------------------------------


def test_cases_are_loaded_by_source_and_sorted(cases_dir: Path) -> None:
    write_case(cases_dir, "py-modnf", case_id="public-x-01", source="public")

    lab = load_cases(cases_dir, ["lab"])
    both = load_cases(cases_dir, ["lab", "public"])

    assert [case.id for case in lab] == sorted(f"lab-{name}-01" for name in SYNTHETIC)
    assert len(both) == len(SYNTHETIC) + 1
    assert lab[0].run_ref().run_id == 123
    assert load_cases(cases_dir, ["injection"]) == []


def test_unknown_sources_and_misplaced_cases_are_rejected(cases_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        load_cases(cases_dir, ["nope"])
    root = write_case(cases_dir, "py-modnf", case_id="public-y-01", source="public")
    (root / "case.yaml").write_text(
        (root / "case.yaml").read_text("utf-8").replace("source: public", "source: lab"), "utf-8"
    )
    with pytest.raises(ValueError, match="stored under 'public'"):
        load_cases(cases_dir, ["public"])


# --- the rules variant -------------------------------------------------------------------------


def test_rules_variant_on_a_case(cases_dir: Path) -> None:
    [case] = [c for c in load_cases(cases_dir) if c.id == "lab-py-modnf-01"]

    result = run_rules(case, COUNTER)

    assert result.correct
    assert result.predicted == "dependency"
    assert result.rule_ids[0] == "PY-MODNF"
    assert set(result.expected_evidence) <= set(result.excerpt_lines)
    assert len(result.signature_hash) == 64
    assert result.error is None


def test_a_case_whose_job_is_missing_is_an_error(cases_dir: Path) -> None:
    [case] = [c for c in load_cases(cases_dir) if c.id == "lab-py-modnf-01"]
    case = type(case)(case.meta.model_copy(update={"job_id": 1}), case.root, case.jobs, case.log)

    result = run_rules(case, COUNTER)

    assert result.error == "no failed job matches the case"
    assert not result.correct


def test_run_evals_produces_a_report(cases_dir: Path) -> None:
    report = run_evals(
        "rules", ["lab"], cases_dir, COUNTER, now=lambda: NOW, git_sha=lambda: "abc1234"
    )

    [run] = report.runs
    assert run.variant == "rules"
    assert run.metrics.cases == len(SYNTHETIC)
    assert run.metrics.accuracy == pytest.approx(4 / 5)  # the npm case is beyond the rules
    assert run.metrics.evidence_in_excerpt == 1.0
    assert report.skipped == {}


def test_all_runs_what_exists_and_lists_the_rest(cases_dir: Path) -> None:
    report = run_evals("all", ["lab"], cases_dir, COUNTER, now=lambda: NOW, git_sha=lambda: "x")

    assert [run.variant for run in report.runs] == ["rules"]
    assert set(report.skipped) == {"llm", "llm+hints", "llm+hints+similar"}


def test_variant_errors() -> None:
    with pytest.raises(EvalError, match="not available yet"):
        resolve_variants("llm")
    with pytest.raises(EvalError, match="unknown variant"):
        resolve_variants("magic")


def test_no_cases_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="no cases"):
        run_evals("rules", ["lab"], tmp_path, COUNTER)


# --- metrics -----------------------------------------------------------------------------------


def test_metrics() -> None:
    results = [
        _result(
            "dependency",
            "dependency",
            0.9,
            latency_ms=10.0,
            expected_evidence=[3, 4],
            excerpt_lines=[1, 2, 3, 4],
            evidence_lines=[3],
        ),
        _result("dependency", "broken_test", 0.85, latency_ms=20.0),
        _result("broken_test", "broken_test", 0.5, latency_ms=30.0),
        _result("flaky", "unknown", 0.0, latency_ms=40.0),
    ]

    m = compute(results)

    assert m.cases == 4
    assert m.accuracy == 0.5
    assert m.per_category["dependency"].precision == 1.0
    assert m.per_category["dependency"].recall == 0.5
    assert m.per_category["broken_test"].precision == 0.5
    assert m.per_category["broken_test"].recall == 1.0
    assert m.per_category["flaky"].f1 == 0.0
    assert m.per_category["unknown"].support == 0
    # macro-F1 over dependency (0.667), broken_test (0.667) and flaky (0)
    assert m.macro_f1 == pytest.approx((2 / 3 + 2 / 3 + 0) / 3, abs=1e-3)
    assert m.confusion == {
        "broken_test": {"broken_test": 1},
        "dependency": {"broken_test": 1, "dependency": 1},
        "flaky": {"unknown": 1},
    }
    assert m.evidence_in_excerpt == 1.0
    assert m.evidence_in_diagnosis == 0.5
    assert m.abstention == 0.25
    assert m.calibration["high"] == {"cases": 2, "accuracy": 0.5}
    assert m.calibration["medium"] == {"cases": 1, "accuracy": 1.0}
    assert m.latency_ms_p50 == 20.0
    assert m.latency_ms_p95 == 40.0


def test_metrics_of_nothing_and_buckets() -> None:
    m = compute([])

    assert (m.cases, m.accuracy, m.macro_f1, m.evidence_in_excerpt) == (0, 0.0, 0.0, None)
    assert [bucket(c) for c in (0.75, 0.74, 0.45, 0.44)] == ["high", "medium", "medium", "low"]


# --- reports and CLI ---------------------------------------------------------------------------


def test_report_files(cases_dir: Path, tmp_path: Path) -> None:
    report = run_evals(
        "rules", ["lab"], cases_dir, COUNTER, now=lambda: NOW, git_sha=lambda: "abc1234"
    )

    json_path, md_path = write_report(report, tmp_path / "reports")

    assert json_path.name == "2026-11-13-abc1234.json"
    data = json.loads(json_path.read_text("utf-8"))
    assert data["variants"]["rules"]["metrics"]["cases"] == len(SYNTHETIC)
    assert len(data["variants"]["rules"]["cases"]) == len(SYNTHETIC)
    markdown = md_path.read_text("utf-8")
    assert markdown == render_markdown(report)
    assert "| rules | 5 | 80.0% |" in markdown
    assert "ModuleNotFoundError" not in markdown  # no log text in reports


def test_cli_run(
    cases_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "TiktokenCounter", CharTokenCounter)
    out = tmp_path / "reports"

    code = cli.main(
        ["run", "--variant", "rules", "--cases-dir", str(cases_dir), "--out-dir", str(out)]
    )

    assert code == 0
    assert "rules: 5 cases, accuracy 80.0%" in capsys.readouterr().out
    assert len(list(out.glob("*.json"))) == 1


def test_cli_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--cases-dir", str(tmp_path)])

    assert code == 1
    assert "error: no cases" in capsys.readouterr().err
