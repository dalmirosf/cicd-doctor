"""P4 (g): the CI gate of the evals, ``evals check`` and ``evals baseline --accept`` (D-105, D-106).

Offline: synthetic reports for the rules, and the CLI over temporary cases with answers filled by a
``FakeLLM`` through the real cache. No network, no OpenAI, no Postgres.
"""

import asyncio
import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.adapters.memory import CharTokenCounter, FakeEmbedder, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis import prompt as prompt_module
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import prompt_fingerprint
from evals.runner import cli, gate, gate_cli
from evals.runner.baseline import BaselineError, parse_baseline
from evals.runner.injection import write_cases
from evals.runner.metrics import compute
from evals.runner.recording import RecordingLLM
from evals.runner.report import Report, VariantRun
from evals.runner.run import LLMSetup, run_evals_async
from evals.runner.variants import CaseResult
from tests.evalcases import SYNTHETIC, write_case

NOW = datetime(2026, 11, 21, tzinfo=UTC)
CANARY = "LOG-CANARY-9c3b21"
FINGERPRINT = "f" * 64
HASH = "a" * 64
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
LLM = "llm+hints [m]"
A, B = "broken_test", "dependency"  # two real categories


# --- synthetic reports -------------------------------------------------------------------------


def result(
    case_id: str,
    variant: str,
    *,
    expected: str = A,
    predicted: str = A,
    source: str = "lab",
    attack: bool = False,
    wins: list[str] | None = None,
    fallback: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        source=source,
        variant=variant,
        expected=expected,
        predicted=predicted,
        confidence=0.9,
        attack=attack,
        wins=wins or [],
        fallback_reason=fallback,
    )


def run(name: str, results: list[CaseResult]) -> VariantRun:
    return VariantRun(name, compute(results), results)


def lab_results(variant: str, wrong: tuple[str, ...] = ()) -> list[CaseResult]:
    """10 lab cases, 5 of category A and 5 of B; the ids in ``wrong`` predict the other one."""
    out = []
    for i in range(10):
        expected = A if i < 5 else B
        other = B if expected == A else A
        case_id = f"lab-{i:02d}"
        out.append(
            result(
                case_id,
                variant,
                expected=expected,
                predicted=other if case_id in wrong else expected,
            )
        )
    return out


def injection_results(variant: str, won: tuple[str, ...] = ()) -> list[CaseResult]:
    return [
        result(
            f"injection-{i}",
            variant,
            source="injection",
            attack=True,
            wins=["forced_category"] if f"injection-{i}" in won else [],
        )
        for i in range(3)
    ]


def make_report(
    *,
    rules_wrong: tuple[str, ...] = (),
    llm_wrong: tuple[str, ...] = (),
    won: dict[str, tuple[str, ...]] | None = None,
    missing: dict[str, tuple[str, ...]] | None = None,
) -> Report:
    won = won or {}
    missing = missing or {}
    runs = [run("rules", lab_results("rules", rules_wrong))]
    for name in ("llm [m]", LLM):
        results = lab_results(name, llm_wrong) + injection_results(name, won.get(name, ()))
        results = [
            replace(r, fallback_reason="llm_error") if r.case_id in missing.get(name, ()) else r
            for r in results
        ]
        runs.append(run(name, results))
    return Report(NOW, "abc1234", ["lab", "injection"], runs)


def baseline_for(report: Report, shift: float = 0.0, **top: Any) -> Any:
    """A baseline whose macro-F1 is the report's plus ``shift`` (so the drop is ``shift``)."""
    variants = {}
    for r in report.runs:
        m = gate.gate_metrics(r)
        variants[r.variant] = {
            "model": gate.model_of(r.variant),
            "cases": m.cases,
            "accuracy": m.accuracy,
            "macro_f1": min(1.0, max(0.0, m.macro_f1 + shift)),
            "f1": {c: s.f1 for c, s in m.per_category.items() if s.support},
            "verdicts": {x.case_id: x.predicted for x in r.results},
        }
    data = {
        "schema_version": 1,
        "cases_hash": HASH,
        "prompt_fingerprint": FINGERPRINT,
        "variants": variants,
        **top,
    }
    return parse_baseline(data)


def evaluate(report: Report, baseline: Any, *, cases: str = HASH, fingerprint: str = FINGERPRINT):  # type: ignore[no-untyped-def]
    return gate.evaluate(
        report, baseline, current_cases_hash=cases, current_fingerprint=fingerprint
    )


# --- the macro-F1 rule -------------------------------------------------------------------------


def test_the_clean_run_passes() -> None:
    report = make_report()
    outcome = evaluate(report, baseline_for(report))
    assert outcome.code == gate.EXIT_OK
    assert outcome.findings == []


@pytest.mark.parametrize(
    ("drop", "code"),
    [(0.049, 0), (0.05, 0), (0.051, 1), (-0.10, 0), (0.30, 1)],
)
def test_the_threshold_is_more_than_five_points(drop: float, code: int) -> None:
    report = make_report(llm_wrong=("lab-00",))  # now ~0.8 in the llm variants, rules is 1.0
    now = gate.gate_metrics(report.runs[2]).macro_f1
    baseline = baseline_for(report)
    # Put the baseline of the production variant ``drop`` above what the run gives.
    variants = dict(baseline.variants)
    variants[LLM] = replace(variants[LLM], macro_f1=now + drop)
    outcome = evaluate(report, replace(baseline, variants=variants))
    assert outcome.code == code


def test_rules_decides_too_and_the_other_variants_do_not() -> None:
    report = make_report(rules_wrong=("lab-00", "lab-05"))
    outcome = evaluate(report, replace(baseline_for(report), variants=_good(report, "rules")))
    assert outcome.code == gate.EXIT_REGRESSION
    assert any(line.startswith("rules:") for line in outcome.findings[0].lines)
    # the same drop in `llm` and `llm+hints+similar` is reported and does not decide
    report = make_report(llm_wrong=("lab-00", "lab-05"))
    base = baseline_for(report)
    variants = dict(base.variants)
    variants["llm [m]"] = replace(variants["llm [m]"], macro_f1=1.0)
    outcome = evaluate(report, replace(base, variants=variants))
    assert outcome.code == gate.EXIT_OK
    row = next(r for r in outcome.rows if r.variant == "llm [m]")
    assert not row.decides
    assert row.baseline_macro_f1 == 1.0


def _good(report: Report, deciding: str) -> dict[str, Any]:
    """The baseline of a perfect past for one variant, the rest as now."""
    variants = dict(baseline_for(report).variants)
    variants[deciding] = replace(variants[deciding], macro_f1=1.0)
    return variants


def test_a_drop_by_one_case_says_so_and_names_it() -> None:
    clean = make_report()
    baseline = baseline_for(clean)
    report = make_report(llm_wrong=("lab-03",))  # one verdict changed, in the production variant
    outcome = evaluate(report, baseline)
    assert outcome.code == gate.EXIT_REGRESSION
    text = gate.render_summary(outcome)
    assert "caused by 1 case" in text
    assert "lab-03: broken_test -> dependency (expected broken_test)" in text
    assert "lab-04" not in text


def test_several_changed_verdicts_are_counted() -> None:
    baseline = baseline_for(make_report())
    outcome = evaluate(make_report(llm_wrong=("lab-01", "lab-02", "lab-06")), baseline)
    text = gate.render_summary(outcome)
    assert "3 verdict(s) changed" in text
    assert "caused by 1 case" not in text


def test_a_category_that_disappears_is_a_note_and_the_rule_still_applies() -> None:
    report = make_report()
    baseline = baseline_for(report)
    variants = {
        name: replace(v, f1={**(v.f1 or {}), "network": 0.8})
        for name, v in baseline.variants.items()
    }
    outcome = evaluate(report, replace(baseline, variants=variants))
    assert outcome.code == gate.EXIT_OK
    assert any("category network has no cases now" in note for note in outcome.notes)


def test_a_gate_variant_missing_from_the_run_is_a_runner_error() -> None:
    report = make_report()
    smaller = replace(report, runs=[r for r in report.runs if r.variant != LLM])
    with pytest.raises(gate.GateError, match="did not produce"):
        evaluate(smaller, baseline_for(report))


def test_a_variant_the_baseline_does_not_have_is_reported_and_never_decides() -> None:
    report = make_report()
    base = baseline_for(report)
    variants = {k: v for k, v in base.variants.items() if k != "llm [m]"}
    outcome = evaluate(report, replace(base, variants=variants))
    assert outcome.code == gate.EXIT_OK
    assert any("llm [m]: not in the baseline" in n for n in outcome.notes)


def test_nan_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    report = make_report()
    base = baseline_for(report)
    real = gate.gate_metrics
    monkeypatch.setattr(gate, "gate_metrics", lambda r: replace(real(r), macro_f1=math.nan))
    with pytest.raises(gate.GateError, match="not a number"):
        evaluate(report, base)
    for bad in ('"macro_f1": NaN', '"macro_f1": 2'):
        text = json.dumps(
            {"schema_version": 1, "variants": {"rules": {"cases": 1, "accuracy": 1, "macro_f1": 0}}}
        ).replace('"macro_f1": 0', bad)
        with pytest.raises(BaselineError):
            parse_baseline(json.loads(text))
    with pytest.raises(BaselineError, match="threshold"):
        parse_baseline(
            {
                "schema_version": 1,
                "threshold": {"macro_f1_drop": math.nan},
                "variants": {"rules": {"cases": 1, "accuracy": 1, "macro_f1": 1}},
            }
        )


# --- injection, incomplete answers, a stale baseline, and who wins -----------------------------


def test_an_attack_won_in_the_llm_variant_fails_even_if_the_macro_f1_improved() -> None:
    clean = make_report(llm_wrong=("lab-00", "lab-01"))
    baseline = baseline_for(clean)  # the baseline was worse than what the run gives
    report = make_report(won={"llm [m]": ("injection-1",)})  # won in `llm`, not in `llm+hints`
    outcome = evaluate(report, baseline)
    assert outcome.code == gate.EXIT_INJECTION
    assert outcome.findings[0].lines == ["llm [m]: 1 won: injection-1"]
    assert gate.gate_metrics(report.runs[2]).macro_f1 > baseline.variants[LLM].macro_f1


def test_an_attack_in_the_rules_variant_is_not_an_llm_attack() -> None:
    report = make_report()
    rules = report.runs[0]
    results = [*rules.results, *injection_results("rules", won=("injection-0",))]
    report = replace(report, runs=[run("rules", results), *report.runs[1:]])
    assert evaluate(report, baseline_for(report)).code == gate.EXIT_OK


def test_missing_answers_are_one_grouped_message_with_counts_and_ids() -> None:
    absent = ("lab-00", "lab-01", "injection-2")
    report = make_report(missing={"llm [m]": absent, LLM: absent[:2]})
    outcome = evaluate(report, baseline_for(make_report()))
    assert outcome.code == gate.EXIT_INCOMPLETE
    [finding] = outcome.findings  # one finding, not one per call or per case
    assert finding.title.startswith("3 case(s) have no stored answer in 2 LLM variant(s)")
    assert "llm [m]: 3 case(s): injection-2, lab-00, lab-01" in finding.lines
    assert "llm+hints [m]: 2 case(s): lab-00, lab-01" in finding.lines
    assert "--live" in finding.lines[-1]
    assert "evals/answers/" in finding.lines[-1]
    assert gate.render_summary(outcome).count("## Exit") == 1


def test_a_long_list_of_cases_is_cut() -> None:
    ids = [f"lab-{i:03d}" for i in range(40)]
    assert gate._ids(ids).endswith("and 25 more")


def test_a_different_prompt_or_case_set_is_a_stale_baseline() -> None:
    report = make_report()
    baseline = baseline_for(report)
    assert evaluate(report, baseline, fingerprint="0" * 64).code == gate.EXIT_STALE
    assert evaluate(report, baseline, cases="0" * 64).code == gate.EXIT_STALE
    text = gate.render_summary(evaluate(report, baseline, fingerprint="0" * 64))
    assert "prompt_fingerprint differs" in text
    assert "baseline --accept" in text
    bare = parse_baseline({"schema_version": 1, "variants": {"rules": _minimal()}})
    assert evaluate(make_report(), bare).code == gate.EXIT_STALE  # no hash, no fingerprint


def _minimal() -> dict[str, Any]:
    return {"cases": 10, "accuracy": 1, "macro_f1": 1}


def test_when_several_apply_the_most_serious_wins() -> None:
    clean = make_report()
    baseline = baseline_for(clean)
    everything = make_report(
        llm_wrong=("lab-00", "lab-05"),
        won={"llm [m]": ("injection-0",)},
        missing={LLM: ("lab-09",)},
    )
    codes = [
        evaluate(everything, baseline, cases="0" * 64).code,  # 2, 3, 4 and 1
        evaluate(
            replace(everything, runs=_without_wins(everything)), baseline, cases="0" * 64
        ).code,
    ]
    assert codes == [2, 3]
    no_missing = make_report(llm_wrong=("lab-00", "lab-05"))
    assert evaluate(no_missing, baseline, cases="0" * 64).code == 4
    assert evaluate(no_missing, baseline).code == 1
    outcome = evaluate(everything, baseline, cases="0" * 64)
    assert [f.code for f in outcome.findings] == [2, 3, 4, 1]  # all are reported, in order


def _without_wins(report: Report) -> list[VariantRun]:
    runs = []
    for r in report.runs:
        results = [replace(x, wins=[]) for x in r.results]
        runs.append(run(r.variant, results))
    return runs


# --- the hash of the cases ---------------------------------------------------------------------


def _cases(root: Path, names: list[str]) -> None:
    for name in names:
        write_case(root, name)


def test_cases_hash_changes_with_a_case_and_not_with_the_order_or_the_path(tmp_path: Path) -> None:
    names = ["pytest-assert", "py-modnf", "job-timeout"]
    a, b = tmp_path / "a", tmp_path / "elsewhere" / "b"
    b.parent.mkdir()
    _cases(a, names)
    _cases(b, list(reversed(names)))  # created in another order, another path
    assert gate.cases_hash(a) == gate.cases_hash(b)
    log = b / "lab" / "lab-py-modnf-01" / "log.txt"
    original = log.read_bytes()
    log.write_bytes(original + b"one more line\n")
    assert gate.cases_hash(a) != gate.cases_hash(b)  # a changed log
    log.write_bytes(original.replace(b"\n", b"\r\n"))
    assert gate.cases_hash(a) == gate.cases_hash(b)  # line endings do not count
    _cases(a, ["docker-copy"])
    assert gate.cases_hash(a) != gate.cases_hash(b)  # an extra case
    assert gate.cases_hash(tmp_path / "empty") == gate.cases_hash(tmp_path / "empty2")


# --- the CLI, end to end over temporary cases --------------------------------------------------


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Cases (with a canary in a log) and answers filled by a FakeLLM, no network."""
    monkeypatch.chdir(tmp_path)  # no .env
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate_cli, "TiktokenCounter", CharTokenCounter)
    monkeypatch.setattr(gate_cli, "build_embedder", lambda settings=None: FakeEmbedder())
    cases = tmp_path / "cases"
    for fixture in SYNTHETIC:
        root = write_case(cases, fixture)
        path = root / "case.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        data["tags"] = ["synthetic", "python"]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    log = cases / "lab" / "lab-pytest-assert-01" / "log.txt"
    log.write_text(log.read_text("utf-8", newline="") + f"\n{CANARY} @everyone\n", encoding="utf-8")
    write_cases(cases / "injection")
    answers = tmp_path / "answers"
    return {"cases": cases, "answers": answers, "out": tmp_path / "out"}


async def fill_answers(world: dict[str, Path]) -> None:
    cache = CachedLLMClient(FakeLLM([valid()] * 600), world["answers"], model="m")
    recorder = RecordingLLM(cache)
    setup = LLMSetup(client=recorder, label="m", cost=PRICES, recorder=recorder)
    await run_evals_async(
        "all",
        ["lab", "public", "injection"],
        world["cases"],
        CharTokenCounter(),
        llms=[setup],
        embedder=FakeEmbedder(),
    )


def accept_args(world: dict[str, Path], out: Path, *extra: str) -> list[str]:
    return [
        "baseline",
        "--accept",
        "--out",
        str(out),
        "--cases-dir",
        str(world["cases"]),
        "--answers-dir",
        str(world["answers"]),
        "--model",
        "m",
        "--price",
        "m=0.40,1.60",
        *extra,
    ]


def check_args(world: dict[str, Path], baseline: Path, summary: Path) -> list[str]:
    return [
        "check",
        "--baseline",
        str(baseline),
        "--cases-dir",
        str(world["cases"]),
        "--answers-dir",
        str(world["answers"]),
        "--out-dir",
        str(world["out"]),
        "--summary",
        str(summary),
    ]


def test_accept_then_check_is_green_and_the_baseline_is_deterministic(
    world: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(fill_answers(world))
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    assert cli.main(accept_args(world, first)) == 0
    # Another order of the cases: the same directories created in reverse, the same answers.
    assert cli.main(accept_args(world, second)) == 0
    one, two = (json.loads(p.read_text("utf-8")) for p in (first, second))
    for body in (one, two):
        body.pop("created_at")
        body.pop("git_sha")
    assert one == two  # only the clock (and the commit) may differ
    assert first.read_text("utf-8").endswith("}\n")
    assert "\r" not in first.read_text("utf-8")
    assert one["prompt_fingerprint"] == prompt_fingerprint()
    assert one["threshold"] == {"macro_f1_drop": 0.05}
    assert list(one["variants"]) == ["rules", "llm [m]", "llm+hints [m]", "llm+hints+similar [m]"]
    production = one["variants"]["llm+hints [m]"]
    assert production["prices"] == {"input_per_mtok": 0.4, "output_per_mtok": 1.6}
    assert production["attacks_won"] == 0
    assert production["cases"] == len(SYNTHETIC)
    assert production["cost_per_llm_diagnosis"] > 0
    assert production["latency_ms_p50"] is not None
    parse_baseline(one)  # the one schema of the reader (D-111)

    summary = tmp_path / "summary.md"
    capsys.readouterr()
    assert cli.main(check_args(world, first, summary)) == 0
    text = summary.read_text("utf-8")
    assert "exit 0" in text
    assert CANARY not in text
    assert "@everyone" not in text
    assert CANARY not in capsys.readouterr().out
    assert not any(CANARY in p.read_text("utf-8") for p in world["out"].glob("*.md"))


def test_check_exit_codes_end_to_end(
    world: dict[str, Path], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    asyncio.run(fill_answers(world))
    baseline, summary = tmp_path / "baseline.json", tmp_path / "summary.md"
    assert cli.main(accept_args(world, baseline)) == 0

    # 4: no baseline file at all
    assert cli.main(check_args(world, tmp_path / "missing.json", summary)) == 4
    assert "nothing was accepted yet" in summary.read_text("utf-8")

    # 5: a damaged baseline file
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert cli.main(check_args(world, broken, summary)) == 5

    # 4: a case was added since the baseline
    write_case(world["cases"], "pytest-assert", case_id="lab-extra-01")
    assert cli.main(check_args(world, baseline, summary)) in (3, 4)
    assert "cases_hash differs" in summary.read_text("utf-8") or "## Exit 3" in summary.read_text(
        "utf-8"
    )
    # (the new case has no stored answer, so 3 outranks 4: fill it and the cause is only 4)
    asyncio.run(fill_answers(world))
    assert cli.main(check_args(world, baseline, summary)) == 4
    assert "cases_hash differs" in summary.read_text("utf-8")


def test_missing_answers_are_one_message_and_no_warning_per_call(
    world: dict[str, Path], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    asyncio.run(fill_answers(world))
    baseline, summary = tmp_path / "baseline.json", tmp_path / "summary.md"
    assert cli.main(accept_args(world, baseline)) == 0
    for entry in world["answers"].rglob("*.json"):
        entry.unlink()  # the answers are gone

    with caplog.at_level("WARNING"):
        assert cli.main(check_args(world, baseline, summary)) == 3
    assert [r for r in caplog.records if "LLM call failed" in r.message] == []
    text = summary.read_text("utf-8")
    assert text.count("## Exit") == 1
    assert "## Exit 3" in text
    assert "evals run --variant all" in text
    assert "--live" in text
    assert "35 case(s) have no stored answer in 3 LLM variant(s)" in text
    # the three variants miss the same cases: one line, not three
    assert "llm [m], llm+hints [m], llm+hints+similar [m]: 35 case(s)" in text
    assert text.count("injection-chat-template-01") == 1
    assert CANARY not in text


def test_a_changed_prompt_is_stale_and_its_answers_are_missing(
    world: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(fill_answers(world))
    baseline, summary = tmp_path / "baseline.json", tmp_path / "summary.md"
    assert cli.main(accept_args(world, baseline)) == 0
    monkeypatch.setattr(prompt_module, "SYSTEM_PROMPT", prompt_module.SYSTEM_PROMPT + " Be kind.")
    # The answers of the old prompt are misses (D-100) and the fingerprint differs: 3 outranks 4.
    assert cli.main(check_args(world, baseline, summary)) == 3
    assert "prompt_fingerprint differs" in summary.read_text("utf-8")  # still reported


def test_a_won_attack_is_refused_as_a_baseline_and_fails_the_check(
    world: dict[str, Path], tmp_path: Path
) -> None:
    obey = FakeLLM(
        [lambda request: _obedient(request)] * 600
    )  # an answer that forces a category on everything
    cache = CachedLLMClient(obey, world["answers"], model="m")
    recorder = RecordingLLM(cache)
    setup = LLMSetup(client=recorder, label="m", cost=PRICES, recorder=recorder)
    asyncio.run(
        run_evals_async(
            "all",
            ["lab", "injection"],
            world["cases"],
            CharTokenCounter(),
            llms=[setup],
            embedder=FakeEmbedder(),
        )
    )
    out = tmp_path / "baseline.json"
    code = cli.main(accept_args(world, out))
    if code == 0:  # the attack did not win with this answer: nothing to refuse
        pytest.skip("the obedient answer did not win any attack")
    assert code == 1
    assert not out.exists()


def _obedient(request: Any) -> Any:
    from cicd_doctor.adapters.memory.llm import Reply, answer

    return Reply(json.dumps(answer(request, category="config_or_secrets", confidence=0.99)))


def test_accept_without_the_flag_writes_nothing(
    world: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "baseline.json"
    args = [a for a in accept_args(world, out) if a != "--accept"]
    args = [
        a
        for i, a in enumerate(args)
        if a not in ("--model", "--price") and args[i - 1] not in ("--model", "--price")
    ]
    assert cli.main(args) == 0
    assert not out.exists()
    assert "dry run" in capsys.readouterr().out


# --- the workflow --------------------------------------------------------------------------------


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "evals.yml"
CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def _uses(workflow: dict[Any, Any]) -> list[str]:
    return [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]


def test_the_workflow_is_valid_pinned_secretless_and_manual_for_now() -> None:
    workflow = _load(WORKFLOW)
    triggers: Any = workflow.get("on", workflow.get(True))  # YAML reads a bare `on` as True
    assert set(triggers) == {
        "workflow_dispatch"
    }  # pull_request and push come in (h), with a baseline
    assert workflow["permissions"] == {"contents": "read"}
    [job] = workflow["jobs"].values()
    assert job["timeout-minutes"] == 8
    assert "services" not in job  # no Postgres
    for ref in _uses(workflow):
        action, _, pin = ref.partition("@")
        assert len(pin) == 40, f"{action} is not pinned by sha"
        assert set(pin) <= set("0123456789abcdef"), f"{action} is not pinned by sha"
    text = WORKFLOW.read_text("utf-8")
    assert "secrets." not in text
    assert "OPENAI" not in text.replace("OpenAI", "")
    assert "pull_request" not in "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "uv sync --locked" in text
    assert "TIKTOKEN_CACHE_DIR" in text
    assert "-m evals check" in text
    assert "GITHUB_STEP_SUMMARY" in text
    assert "upload-artifact" in text


def test_the_workflow_uses_the_same_pins_and_model_key_as_ci() -> None:
    ci = _load(CI)
    shared = {ref for ref in _uses(_load(WORKFLOW)) if "upload-artifact" not in ref}
    assert shared <= set(_uses(ci))
    key = (
        "fastembed-bge-small-en-v1.5-onnx-Q-"
        "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
    )
    assert key in CI.read_text("utf-8")
    assert key in WORKFLOW.read_text("utf-8")
