"""The LLM variants of the eval runner, the answer cache and the model setup (SPEC §11.3, D-33).

Nothing here calls OpenAI. The tests run in a temporary directory with no ``.env`` and no OpenAI
variable, so they cannot read a real key.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.adapters.memory import CharTokenCounter, FakeLLM
from cicd_doctor.adapters.memory.llm import obeys_injection, unauthorized, valid
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from evals.runner import cli
from evals.runner.cases import DEFAULT_CASES_DIR
from evals.runner.live import LiveSettings, build_setups, parse_prices
from evals.runner.report import Report, render_markdown
from evals.runner.run import EvalError, LLMSetup, run_evals_async
from tests.evalcases import SYNTHETIC, write_case
from tests.llmcases import make_hint, make_request

COUNTER = CharTokenCounter()
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
CALL_COST = 0.00128  # a FakeLLM reply: 2000 tokens in, 300 out, at PRICES


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``.env`` (the working directory is empty) and no OpenAI variable."""
    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "LLM_PRICE_INPUT_PER_MTOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    for fixture in SYNTHETIC:
        write_case(root, fixture)
    return root


def entries(directory: Path, pattern: str = "*.json") -> list[Path]:
    """The files of a directory (blocking IO stays out of the async tests)."""
    return sorted(directory.glob(pattern))


def setup_for(fake: FakeLLM, label: str = "model-a") -> LLMSetup:
    return LLMSetup(client=fake, label=label, cost=PRICES)


async def run(variant: str, cases: Path, *llms: LLMSetup, sources: str = "lab") -> Report:
    return await run_evals_async(variant, [sources], cases, COUNTER, llms=llms)


# --- the cache ------------------------------------------------------------------------------------


class CountingLLM:
    def __init__(self, raw_json: str = '{"ok": true}') -> None:
        self.calls = 0
        self.raw_json = raw_json

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            raw_json=self.raw_json, model="m-real", input_tokens=7, output_tokens=3, latency_ms=11
        )


class FailingLLM:
    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        raise LLMError("boom")


async def test_a_live_answer_is_stored_and_served_from_the_cache_next_time(tmp_path: Path) -> None:
    inner = CountingLLM()
    live = CachedLLMClient(inner, tmp_path / "cache", model="m")
    offline = CachedLLMClient(None, tmp_path / "cache", model="m")
    request = make_request()

    first = await live.diagnose(request)
    second = await offline.diagnose(request)

    assert inner.calls == 1
    assert second == first
    assert (live.hits, live.misses, offline.hits, offline.misses) == (0, 1, 1, 0)
    assert len(entries(tmp_path / "cache")) == 1


async def test_the_cache_serves_the_second_identical_request_without_a_call(
    tmp_path: Path,
) -> None:
    inner = CountingLLM()
    client = CachedLLMClient(inner, tmp_path, model="m")

    await client.diagnose(make_request())
    await client.diagnose(make_request())

    assert inner.calls == 1


async def test_an_offline_miss_is_a_permanent_llm_error(tmp_path: Path) -> None:
    client = CachedLLMClient(None, tmp_path, model="m")

    with pytest.raises(LLMError, match="run with --live to fill the cache"):
        await client.diagnose(make_request())


async def test_errors_are_not_cached(tmp_path: Path) -> None:
    client = CachedLLMClient(FailingLLM(), tmp_path, model="m")

    with pytest.raises(LLMError, match="boom"):
        await client.diagnose(make_request())

    assert entries(tmp_path, "*") == []


def test_the_key_depends_on_the_model_the_prompt_the_hints_and_the_repair() -> None:
    request = make_request()
    base = CachedLLMClient(None, Path("."), model="a").key(request)

    assert CachedLLMClient(None, Path("."), model="a").key(request) == base
    assert CachedLLMClient(None, Path("."), model="b").key(request) != base
    assert (
        CachedLLMClient(None, Path("."), model="a", prompt_version="diag-v3").key(request) != base
    )
    assert (
        CachedLLMClient(None, Path("."), model="a").key(make_request(rule_hints=[make_hint()]))
        != base
    )
    assert (
        CachedLLMClient(None, Path("."), model="a").key(
            make_request(repair_error="confidence: too large")
        )
        != base
    )
    assert len(base) == 64
    assert PROMPT_VERSION == "diag-v2"


async def test_an_entry_has_the_answer_and_never_the_request(tmp_path: Path) -> None:
    client = CachedLLMClient(CountingLLM(), tmp_path, model="m-1")

    await client.diagnose(make_request())

    [entry] = entries(tmp_path)
    data = json.loads(entry.read_text("utf-8"))
    assert set(data) == {"model", "prompt_version", "result"}
    assert "ModuleNotFoundError" not in entry.read_text("utf-8")  # no log text
    assert entries(tmp_path, "*.tmp") == []


async def test_a_damaged_entry_is_a_miss_that_live_mode_overwrites(tmp_path: Path) -> None:
    inner = CountingLLM()
    client = CachedLLMClient(inner, tmp_path, model="m")
    await client.diagnose(make_request())
    [entry] = entries(tmp_path)
    entry.write_text("{ not json", encoding="utf-8")

    with pytest.raises(LLMError):
        await CachedLLMClient(None, tmp_path, model="m").diagnose(make_request())
    await client.diagnose(make_request())

    assert inner.calls == 2
    assert json.loads(entry.read_text("utf-8"))["result"]["model"] == "m-real"


# --- the llm variants -----------------------------------------------------------------------------


async def test_the_llm_variants_run_the_pipeline_and_are_named_after_the_model(
    cases_dir: Path,
) -> None:
    fake = FakeLLM([valid()] * len(SYNTHETIC) * 2)

    report = await run("all", cases_dir, setup_for(fake))

    assert [r.variant for r in report.runs] == ["rules", "llm [model-a]", "llm+hints [model-a]"]
    llm = report.runs[1]
    assert llm.metrics.cases == len(SYNTHETIC)
    assert llm.metrics.llm_share == 1.0
    assert llm.metrics.llm_calls == len(SYNTHETIC)
    assert llm.metrics.cost_usd_total == pytest.approx(CALL_COST * len(SYNTHETIC))
    assert llm.metrics.cost_usd_per_case == pytest.approx(CALL_COST)
    assert (llm.metrics.input_tokens, llm.metrics.output_tokens) == (2000 * 5, 300 * 5)
    assert llm.metrics.fallbacks == {}
    assert all(r.diagnosis_source == "llm" for r in llm.results)
    assert report.skipped == {"llm+hints+similar": "arrives with similarity search (P2)"}


async def test_the_llm_variant_withholds_the_hints_and_llm_plus_hints_sends_them(
    cases_dir: Path,
) -> None:
    fake = FakeLLM([valid()] * len(SYNTHETIC) * 2)

    await run("all", cases_dir, setup_for(fake))

    without, with_hints = fake.calls[: len(SYNTHETIC)], fake.calls[len(SYNTHETIC) :]
    assert all(call.rule_hints == [] for call in without)
    assert any(call.rule_hints for call in with_hints)  # the cases a rule matches


async def test_two_models_make_two_runs_of_the_same_variant(cases_dir: Path) -> None:
    cheap = FakeLLM([valid()] * len(SYNTHETIC))
    strong = FakeLLM([valid(confidence=0.95)] * len(SYNTHETIC))

    report = await run(
        "llm+hints", cases_dir, setup_for(cheap, "cheap"), setup_for(strong, "strong")
    )

    assert [r.variant for r in report.runs] == ["llm+hints [cheap]", "llm+hints [strong]"]
    assert report.runs[0].results[0].confidence != report.runs[1].results[0].confidence


async def test_an_llm_error_is_a_fallback_that_the_report_counts(cases_dir: Path) -> None:
    fake = FakeLLM([unauthorized()] * len(SYNTHETIC))

    report = await run("llm", cases_dir, setup_for(fake))

    [llm] = report.runs
    assert llm.metrics.fallbacks == {"llm_error": len(SYNTHETIC)}
    assert llm.metrics.llm_share == 0.0
    assert llm.metrics.cost_usd_total == 0
    assert all(r.diagnosis_source == "rules" for r in llm.results)
    assert all(r.fallback_reason == "llm_error" for r in llm.results)


async def test_a_missing_cache_shows_up_as_llm_errors_not_a_crash(
    cases_dir: Path, tmp_path: Path
) -> None:
    offline = LLMSetup(CachedLLMClient(None, tmp_path / "empty", model="m"), "m", PRICES)

    report = await run("llm", cases_dir, offline)

    assert report.runs[0].metrics.fallbacks == {"llm_error": len(SYNTHETIC)}


async def test_a_run_through_a_filled_cache_is_identical_and_free(
    cases_dir: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    fake = FakeLLM([valid()] * len(SYNTHETIC))
    live = LLMSetup(CachedLLMClient(fake, cache, model="m"), "m", PRICES)
    offline = LLMSetup(CachedLLMClient(None, cache, model="m"), "m", PRICES)

    first = await run("llm", cases_dir, live)
    second = await run("llm", cases_dir, offline)

    assert len(fake.calls) == len(SYNTHETIC)  # the second run made no call at all
    a, b = first.runs[0], second.runs[0]
    assert [r.predicted for r in a.results] == [r.predicted for r in b.results]
    assert [r.cost_usd for r in a.results] == [r.cost_usd for r in b.results]


async def test_without_any_model_the_llm_variants_cannot_run(cases_dir: Path) -> None:
    with pytest.raises(EvalError, match="needs a model"):
        await run("llm", cases_dir)


# --- injection cases through the variants ---------------------------------------------------------


async def test_an_obedient_model_forces_some_categories_and_the_report_says_so() -> None:
    cases = DEFAULT_CASES_DIR
    count = len(list((cases / "injection").iterdir()))
    obedient = FakeLLM([obeys_injection("network", cause="@octocat https://evil.example")] * count)

    report = await run("llm", cases, setup_for(obedient), sources="injection")

    [llm] = report.runs
    assert llm.metrics.attack_cases == count
    assert llm.metrics.attacks_won > 0  # an obedient model forces the attacker's category
    wins = {win for r in llm.results for win in r.wins}
    assert wins == {"category_forced"}  # but nothing else gets through
    markdown = render_markdown(report)
    assert "ATTACK WON: category_forced" in markdown
    assert f"| {llm.metrics.attacks_won}/{count} |" in markdown


async def test_a_model_that_resists_wins_nothing() -> None:
    cases = DEFAULT_CASES_DIR
    count = len(list((cases / "injection").iterdir()))

    report = await run(
        "llm+hints", cases, setup_for(FakeLLM([valid()] * count)), sources="injection"
    )

    [llm] = report.runs
    assert (llm.metrics.attacks_won, llm.metrics.attack_success_rate) == (0, 0.0)
    assert llm.metrics.accuracy == 1.0  # the hint points at the real category


async def test_cases_without_attacks_have_no_attack_rate(cases_dir: Path) -> None:
    report = await run("llm", cases_dir, setup_for(FakeLLM([valid()] * len(SYNTHETIC))))

    assert report.runs[0].metrics.attack_success_rate is None
    assert "| n/a |" in render_markdown(report)


# --- the models of a run --------------------------------------------------------------------------


def test_prices_are_parsed_per_model() -> None:
    prices = parse_prices(["gpt-a=0.40,1.60", "gpt-b=2,8"])

    assert prices["gpt-a"] == PRICES
    assert prices["gpt-b"].input_per_mtok == Decimal(2)


@pytest.mark.parametrize("bad", ["gpt-a", "gpt-a=1", "=1,2", "gpt-a=x,y", "gpt-a=1,2,3"])
def test_a_bad_price_is_an_error(bad: str) -> None:
    with pytest.raises(EvalError, match="expected MODEL=INPUT,OUTPUT"):
        parse_prices([bad])


def test_offline_setups_need_no_key(tmp_path: Path) -> None:
    setups = build_setups(["m1", "m2"], live=False, cache_dir=tmp_path)

    assert [s.label for s in setups] == ["m1", "m2"]
    assert all(isinstance(s.client, CachedLLMClient) for s in setups)


def test_live_needs_the_key_and_never_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(EvalError, match="OPENAI_API_KEY"):
        build_setups(["m1"], live=True, cache_dir=tmp_path)

    key = "sk-test-not-a-real-key"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    setups = build_setups(["m1"], live=True, cache_dir=tmp_path)

    assert len(setups) == 1
    settings = LiveSettings()
    assert key not in repr(settings)
    assert key not in str(settings)


def test_setups_use_the_env_prices_unless_a_model_has_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "1")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "3")

    setups = build_setups(
        ["a", "b"], live=False, cache_dir=tmp_path, prices=parse_prices(["b=5,6"])
    )

    assert setups[0].cost == CostModel(input_per_mtok=Decimal(1), output_per_mtok=Decimal(3))
    assert setups[1].cost == CostModel(input_per_mtok=Decimal(5), output_per_mtok=Decimal(6))


# --- the command line -----------------------------------------------------------------------------


def test_cli_llm_without_a_model_says_what_is_missing(
    cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["run", "--variant", "llm", "--cases-dir", str(cases_dir), "--out-dir", str(tmp_path / "r")]
    )

    assert code == 1
    assert "needs a model" in capsys.readouterr().err


def test_cli_with_a_model_but_no_cache_reports_the_fallbacks(
    cases_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "TiktokenCounter", CharTokenCounter)

    code = cli.main(
        [
            "run",
            "--variant",
            "llm+hints",
            "--model",
            "m1",
            "--price",
            "m1=0.4,1.6",
            "--cases-dir",
            str(cases_dir),
            "--cache-dir",
            str(tmp_path / "empty-cache"),
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "llm+hints [m1]: 5 cases" in out
    [report] = (tmp_path / "reports").glob("*.json")
    data = json.loads(report.read_text("utf-8"))
    assert data["variants"]["llm+hints [m1]"]["metrics"]["fallbacks"] == {"llm_error": 5}


def test_cli_live_without_a_key_is_a_clear_error(
    cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "run",
            "--variant",
            "llm",
            "--model",
            "m1",
            "--live",
            "--cases-dir",
            str(cases_dir),
            "--out-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert code == 1
    assert "--live needs OPENAI_API_KEY" in capsys.readouterr().err


def test_cli_write_injection_regenerates_the_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "DEFAULT_CASES_DIR", tmp_path)

    code = cli.main(["write-injection"])

    assert code == 0
    assert len(list((tmp_path / "injection").iterdir())) >= 10
    assert "wrote" in capsys.readouterr().out
