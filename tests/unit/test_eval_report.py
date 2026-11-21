"""P4 (f): the full ``--variant all`` report, its JSON schema, the baseline reader, the verdict
changes and the README table (SPEC §11.2, §11.3). Offline: FakeLLM, FakeJudge, no network."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from cicd_doctor.adapters.memory import CharTokenCounter, FakeEmbedder, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis.cost import CostModel
from evals.runner import cli
from evals.runner.baseline import (
    DEFAULT_BASELINE,
    BaselineError,
    load_baseline,
    parse_baseline,
)
from evals.runner.cases import load_cases
from evals.runner.injection import write_cases
from evals.runner.judge import (
    JUDGE_PROMPT_VERSION,
    CauseMatch,
    FakeJudge,
    judge_fingerprint,
    judge_results,
)
from evals.runner.readme_table import (
    DEFAULT_README,
    END,
    PLACEHOLDER,
    START,
    ReadmeError,
    check_readme,
    nfr_03_line,
    render_table,
    replace_block,
    write_readme,
)
from evals.runner.recording import RecordingLLM
from evals.runner.report import (
    METRIC_ROWS,
    Report,
    ecosystem_group,
    render_markdown,
    verdict_changes,
    write_report,
)
from evals.runner.report_schema import REPORT_SCHEMA, validate
from evals.runner.run import LLMSetup, run_evals, run_evals_async
from tests.evalcases import SYNTHETIC, write_case

NOW = datetime(2026, 11, 21, tzinfo=UTC)
CANARY = "LOG-CANARY-7d41e0"
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
TAGS = {
    "pytest-assert": ["python", "pytest"],
    "py-modnf": ["python"],
    "job-timeout": ["timeout"],
    "docker-copy": ["docker"],
    "no-step-data": ["network"],
}


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    for fixture in SYNTHETIC:
        root = write_case(cases, fixture)
        path = root / "case.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        data["tags"] = ["synthetic", *TAGS[fixture]]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    # A line no report may repeat: logs are untrusted text.
    log = cases / "lab" / "lab-pytest-assert-01" / "log.txt"
    log.write_text(log.read_text("utf-8", newline="") + f"\n{CANARY} @everyone\n", encoding="utf-8")
    write_cases(cases / "injection")
    return cases


async def _full_report(cases: Path, *, judge: bool = True) -> Report:
    fake = FakeLLM([valid()] * 400)
    recorder = RecordingLLM(fake)
    setup = LLMSetup(client=recorder, label="m", cost=PRICES, recorder=recorder)
    report = await run_evals_async(
        "all",
        ["lab", "public", "injection"],
        cases,
        CharTokenCounter(),
        llms=[setup],
        embedder=FakeEmbedder(),
        now=lambda: NOW,
        git_sha=lambda: "abc1234",
    )
    if not judge:
        return report
    loaded = load_cases(cases, ["lab", "public", "injection"])
    results = [r for run in report.runs for r in run.results]
    judgments = await judge_results(results, loaded, FakeJudge(2))
    return replace(
        report,
        cause_match=CauseMatch("judge-m", JUDGE_PROMPT_VERSION, judge_fingerprint(), judgments),
    )


# --- the whole report --------------------------------------------------------------------------


async def test_the_full_report_has_every_row_of_the_table_of_section_11_2(cases_dir: Path) -> None:
    report = await _full_report(cases_dir)
    markdown = render_markdown(report)
    data = report.to_dict()

    assert len(METRIC_ROWS) == 11  # the 9 rows of §11.2, two of them spelled out in two parts
    for metric, heading, key in METRIC_ROWS:
        assert heading in markdown, metric
        assert data[key], metric
    for variant in (
        "rules",
        "llm [m]",
        "llm+hints [m]",
        "llm+hints+similar [m]",
    ):
        assert variant in data["variants"], variant  # type: ignore[operator]


async def test_every_section_and_metric_is_in_the_markdown_and_json(cases_dir: Path) -> None:
    report = await _full_report(cases_dir)
    markdown = render_markdown(report)
    data = report.to_dict()

    for heading in (
        "## Summary",
        "## Verdict changes against the baseline",
        "## Cause match",
        "## Flaky and fix resolution",
        "## Injection resistance",
        "## By source",
        "## By ecosystem tag",
        "## Evidence and abstention",
        "## Cost and latency",
        "## Bootstrap intervals",
        "## Detail: rules",
        "## Detail: llm+hints [m]",
    ):
        assert heading in markdown, heading
    for column in (
        "Accuracy",
        "Macro-F1",
        "Cause match",
        "Evidence validity",
        "Expected evidence cited",
        "Abstention",
        "Model call p50 / p95",
        "Pipeline p50 / p95",
        "Cost per LLM diagnosis",
        "Total cost",
        "ECE",
        "Precision",
        "Confusion matrix",
        "By decile",
        "rules-known",
        "outside the rules",
        "Attacks won",
    ):
        assert column in markdown, column
    metrics = data["variants"]["llm+hints [m]"]["metrics"]  # type: ignore[index]
    for key in (
        "accuracy",
        "macro_f1",
        "per_category",
        "confusion",
        "evidence_validity",
        "evidence_in_diagnosis",
        "abstention_unknown",
        "calibration",
        "expected_calibration_error",
        "cost_usd_per_llm_diagnosis",
        "cost_usd_p95",
        "cost_usd_total",
        "llm_latency_ms_p50",
        "latency_ms_p95",
        "intervals",
    ):
        assert key in metrics, key
    assert data["flaky_sequences"]["accuracy"] == 1.0  # type: ignore[index]
    assert data["cause_match"]["stats"]  # type: ignore[index]
    assert data["injection"]["rules"]["attack_cases"] == 30  # type: ignore[index]


async def test_the_summary_is_short_and_the_detail_has_one_section_per_variant(
    cases_dir: Path,
) -> None:
    markdown = render_markdown(await _full_report(cases_dir))
    summary = markdown.split("## Summary")[1].split("\n## ")[0]

    assert (
        len([ln for ln in summary.splitlines() if ln.startswith("| ")]) == 1 + 4
    )  # header and the 4 variants
    assert max(len(ln.split("|")) for ln in summary.splitlines() if ln.startswith("|")) <= 10
    assert markdown.count("## Detail: ") == 4


async def test_sections_per_source_and_per_tag(cases_dir: Path) -> None:
    report = await _full_report(cases_dir)
    data = report.to_dict()

    assert set(data["by_source"]["rules"]) == {"lab", "injection"}  # type: ignore[index]
    assert report.empty_sources == ["public"]
    assert "No cases yet for: public." in render_markdown(report)
    groups = data["by_tag_group"]["rules"]  # type: ignore[index]
    assert groups["rules_known"]["cases"] == 3  # python x2 and docker
    assert groups["outside_rules"]["cases"] == 2  # timeout and network
    assert "injection" not in data["by_tag"]["rules"]  # type: ignore[index]
    assert data["by_tag"]["rules"]["python"]["cases"] == 2  # type: ignore[index]
    assert "synthetic" not in data["by_tag"]["rules"]  # type: ignore[index]
    assert ecosystem_group(["terraform"]) == "rules_known"
    assert ecosystem_group(["gate", "flaky"]) == "outside_rules"
    assert ecosystem_group([]) == "outside_rules"


async def test_the_json_validates_against_the_declared_schema(
    cases_dir: Path, tmp_path: Path
) -> None:
    report = await _full_report(cases_dir)
    json_path, _ = write_report(report, tmp_path / "reports")
    data = json.loads(json_path.read_text("utf-8"))

    assert validate(data, REPORT_SCHEMA) == []
    unjudged = await _full_report(cases_dir, judge=False)
    assert validate(json.loads(json.dumps(unjudged.to_dict())), REPORT_SCHEMA) == []


def test_the_schema_validator_catches_a_wrong_report() -> None:
    assert validate({"schema_version": 3}, REPORT_SCHEMA)
    assert any("missing" in p for p in validate({"schema_version": 2}, REPORT_SCHEMA))
    assert validate({"a": "x"}, {"type": "object", "additionalProperties": {"type": "integer"}})
    assert validate([1, "x"], {"type": "array", "items": {"type": "integer"}}) == [
        "$[1]: expected integer, got str"
    ]
    assert validate(True, {"type": "integer"})  # a bool is not a number


async def test_no_report_contains_log_text(cases_dir: Path, tmp_path: Path) -> None:
    report = await _full_report(cases_dir)
    json_path, md_path = write_report(report, tmp_path / "reports")

    for text in (json_path.read_text("utf-8"), md_path.read_text("utf-8")):
        assert CANARY not in text
        assert "@everyone" not in text
        assert "ModuleNotFoundError" not in text


def test_without_the_judge_and_without_a_model_the_report_says_what_is_missing(
    cases_dir: Path,
) -> None:
    report = run_evals(
        "all",
        ["lab", "public"],
        cases_dir,
        CharTokenCounter(),
        now=lambda: NOW,
        git_sha=lambda: "x",
    )
    markdown = render_markdown(report)

    assert [run.variant for run in report.runs] == ["rules"]
    assert "not run: needs a model" in markdown
    assert "Not judged: run with `--judge`" in markdown
    assert "No injection cases in this run" in markdown
    assert validate(json.loads(json.dumps(report.to_dict())), REPORT_SCHEMA) == []


# --- verdict changes -----------------------------------------------------------------------------


def _baseline(verdicts: dict[str, str] | None, variant: str = "rules") -> object:
    body: dict[str, object] = {
        "cases": 5,
        "accuracy": 0.8,
        "macro_f1": 0.7,
        "model": None,
    }
    if verdicts is not None:
        body["verdicts"] = verdicts
    return parse_baseline({"schema_version": 1, "variants": {variant: body}})


async def test_verdict_changes_without_a_baseline_say_so(cases_dir: Path) -> None:
    report = await _full_report(cases_dir, judge=False)

    changes = verdict_changes(report)
    assert changes["baseline"] is False
    assert "no accepted baseline" in str(changes["reason"])
    assert "No baseline to compare with" in render_markdown(report)


async def test_verdict_changes_list_ids_and_categories_only(cases_dir: Path) -> None:
    report = await _full_report(cases_dir, judge=False)
    rules = next(run for run in report.runs if run.variant == "rules")
    verdicts = {r.case_id: r.predicted for r in rules.results}
    verdicts["lab-py-modnf-01"] = "network"  # the baseline said something else
    verdicts["gone-01"] = "dependency"
    del verdicts["lab-job-timeout-01"]
    with_baseline = replace(report, baseline=_baseline(verdicts))  # type: ignore[arg-type]

    changes = verdict_changes(with_baseline)
    body = changes["variants"]["rules"]  # type: ignore[index]
    assert [c["case_id"] for c in body["changes"]] == ["lab-py-modnf-01"]
    assert (body["changes"][0]["from"], body["changes"][0]["to"]) == ("network", "dependency")
    assert body["added"] != []
    assert "lab-job-timeout-01" in body["added"]
    assert body["removed"] == ["gone-01"]
    assert changes["not_in_baseline"] == [
        "llm [m]",
        "llm+hints [m]",
        "llm+hints+similar [m]",
    ]
    markdown = render_markdown(with_baseline)
    assert "| lab-py-modnf-01 | network | dependency |" in markdown
    assert CANARY not in markdown
    assert validate(json.loads(json.dumps(with_baseline.to_dict())), REPORT_SCHEMA) == []


async def test_a_baseline_without_verdicts_is_listed(cases_dir: Path) -> None:
    report = replace(
        await _full_report(cases_dir, judge=False),
        baseline=_baseline(None),  # type: ignore[arg-type]
    )

    assert verdict_changes(report)["without_verdicts"] == ["rules"]
    assert "Baseline has no per-case verdicts for rules." in render_markdown(report)


# --- the baseline reader -----------------------------------------------------------------------


def test_the_baseline_reader_accepts_the_documented_shape_and_extra_keys() -> None:
    raw = {
        "schema_version": 1,
        "cases_hash": "ignored",
        "variants": {
            "llm+hints [gpt-x]": {
                "model": "gpt-x",
                "cases": 55,
                "accuracy": 0.9,
                "macro_f1": 0.88,
                "cause_match_mean": 1.6,
                "cost_per_llm_diagnosis": 0.0004,
                "latency_ms_p50": 900,
                "latency_ms_p95": 2100,
                "prices": {"input_per_mtok": 0.1, "output_per_mtok": 0.4},
                "verdicts": {"a": "dependency"},
                "extra": 1,
            }
        },
    }

    variant = parse_baseline(raw).variants["llm+hints [gpt-x]"]
    assert (variant.cases, variant.macro_f1, variant.latency_ms_p95) == (55, 0.88, 2100.0)
    assert variant.prices is not None
    assert variant.prices.output_per_mtok == 0.4
    assert variant.verdicts == {"a": "dependency"}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"schema_version": 9, "variants": {"a": {}}},
        {"schema_version": 1},
        {"schema_version": 1, "variants": {}},
        {"schema_version": 1, "variants": {"a": []}},
        {"schema_version": 1, "variants": {"a": {"cases": 1, "accuracy": 2, "macro_f1": 0.5}}},
        {"schema_version": 1, "variants": {"a": {"cases": -1, "accuracy": 1, "macro_f1": 0.5}}},
        {"schema_version": 1, "variants": {"a": {"cases": 1, "accuracy": True, "macro_f1": 0.5}}},
        {
            "schema_version": 1,
            "variants": {
                "a": {"cases": 1, "accuracy": 1, "macro_f1": 1, "verdicts": {"a": 1}},
            },
        },
        {
            "schema_version": 1,
            "variants": {"a": {"cases": 1, "accuracy": 1, "macro_f1": 1, "prices": {}}},
        },
        {
            "schema_version": 1,
            "variants": {"a": {"cases": 1, "accuracy": 1, "macro_f1": 1, "model": 3}},
        },
    ],
)
def test_the_baseline_reader_rejects_what_the_schema_does_not_allow(raw: object) -> None:
    with pytest.raises(BaselineError):
        parse_baseline(raw)


def test_load_baseline(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(BaselineError, match="not valid JSON"):
        load_baseline(bad)


# --- the README table ------------------------------------------------------------------------


GOOD = {
    "schema_version": 1,
    "variants": {
        "rules": {"model": None, "cases": 55, "accuracy": 0.75, "macro_f1": 0.7},
        "llm+hints [m]": {
            "model": "m",
            "cases": 55,
            "accuracy": 0.9,
            "macro_f1": 0.875,
            "cause_match_mean": 1.5,
            "cost_per_llm_diagnosis": 0.0004,
            "latency_ms_p50": 900,
            "latency_ms_p95": 2100,
            "prices": {"input_per_mtok": 0.1, "output_per_mtok": 0.4},
        },
    },
}


def test_the_table_has_every_column_and_the_nfr_03_line() -> None:
    table = render_table(parse_baseline(GOOD))

    assert (
        "| Variant | Model | N | Accuracy | Macro-F1 | Cause match (mean 0-2) "
        "| Cost per LLM diagnosis (USD) | Model call p50 / p95 (ms) |" in table
    )
    assert "| rules | - | 55 | 75.0% | 0.700 | n/a | n/a | n/a |" in table
    assert "| llm+hints [m] | m | 55 | 90.0% | 0.875 | 1.50 | 0.00040 | 900 / 2100 |" in table
    assert "NFR-03 (average cost <= USD 0.01 per LLM diagnosis): met by llm+hints [m]." in table


def test_nfr_03_says_when_it_is_not_met_or_not_measured() -> None:
    over = json.loads(json.dumps(GOOD))
    over["variants"]["llm+hints [m]"]["cost_per_llm_diagnosis"] = 0.02
    over["variants"]["llm+hints [m]"]["prices"] = None
    line = nfr_03_line(parse_baseline(over))
    assert "**not met** by llm+hints [m] (0.02000)" in line
    assert "No prices recorded for llm+hints [m]" in line
    rules_only = {"schema_version": 1, "variants": {"rules": GOOD["variants"]["rules"]}}  # type: ignore[index]
    assert "not measured" in nfr_03_line(parse_baseline(rules_only))


def _readme(body: str = "old") -> str:
    return f"# T\n\nbefore\n\n{START}\n{body}\n{END}\n\nafter\n"


def test_replace_block_is_idempotent_and_keeps_the_rest() -> None:
    baseline = parse_baseline(GOOD)
    once = replace_block(_readme(), baseline)
    assert replace_block(once, baseline) == once
    assert once.startswith("# T\n\nbefore\n\n" + START + "\n")
    assert once.endswith(END + "\n\nafter\n")
    assert "| rules | - | 55 |" in once
    placeholder = replace_block(once, None)
    assert PLACEHOLDER in placeholder
    assert replace_block(placeholder, None) == placeholder


@pytest.mark.parametrize(
    "text", ["no markers", f"{START} only", f"{START}\n{END}\n{START}\n{END}", f"{END}\n{START}"]
)
def test_broken_markers_are_an_error(text: str) -> None:
    with pytest.raises((ReadmeError, ValueError)):
        replace_block(text, None)


def test_write_and_check_readme(tmp_path: Path) -> None:
    readme, baseline = tmp_path / "README.md", tmp_path / "baseline.json"
    readme.write_text(_readme(), encoding="utf-8", newline="\n")
    assert not check_readme(readme, baseline)
    assert write_readme(readme, baseline) is True  # placeholder written
    assert check_readme(readme, baseline)
    assert write_readme(readme, baseline) is False  # idempotent
    baseline.write_text(json.dumps(GOOD), encoding="utf-8")
    assert not check_readme(readme, baseline)
    assert write_readme(readme, baseline) is True
    assert check_readme(readme, baseline)


def test_the_readme_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    readme, baseline = tmp_path / "README.md", tmp_path / "baseline.json"
    readme.write_text(_readme(), encoding="utf-8", newline="\n")
    args = ["readme-table", "--readme", str(readme), "--baseline", str(baseline)]

    assert cli.main([*args, "--check"]) == 1
    assert cli.main(args) == 0
    assert "updated" in capsys.readouterr().out
    assert cli.main([*args, "--check"]) == 0
    baseline.write_text("[]", encoding="utf-8")
    assert cli.main(args) == 1  # a bad baseline is an error, not a crash


def test_the_readme_of_the_repository_matches_the_baseline() -> None:
    """Like the injection drift test: run ``python -m evals readme-table`` when it fails."""
    assert check_readme(DEFAULT_README, DEFAULT_BASELINE), (
        "README.md is out of date with evals/baseline.json: run "
        "`uv run python -m evals readme-table`"
    )
    text = DEFAULT_README.read_text("utf-8")
    assert text.count(START) == 1
    assert text.count(END) == 1
    if not DEFAULT_BASELINE.exists():
        assert PLACEHOLDER in text  # nothing accepted yet: the table says so


# --- the CLI ---------------------------------------------------------------------------------


def test_cli_run_all_writes_both_files(
    cases_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "TiktokenCounter", CharTokenCounter)
    out = tmp_path / "out"

    code = cli.main(
        [
            "run",
            "--variant",
            "all",
            "--sources",
            "lab,public,injection",
            "--cases-dir",
            str(cases_dir),
            "--out-dir",
            str(out),
            "--baseline",
            str(tmp_path / "none.json"),
            "--answers-dir",
            str(tmp_path / "answers"),
        ]
    )

    assert code == 0
    [json_path] = out.glob("*.json")
    [md_path] = out.glob("*.md")
    assert validate(json.loads(json_path.read_text("utf-8")), REPORT_SCHEMA) == []
    assert "No baseline to compare with" in md_path.read_text("utf-8")
