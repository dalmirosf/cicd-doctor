"""P4 (h): ``evals run --estimate`` and ``evals variance`` (D-112).

Offline: nothing here calls a model. The estimate runs the real pipeline over temporary cases with
the answer cache empty (every case is a miss) or filled (every case is a hit).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.adapters.memory import CharTokenCounter, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.errors import LLMError
from evals.runner import cli
from evals.runner.estimate import (
    Estimate,
    EstimatingLLM,
    build_estimating_setups,
    estimate_of,
    render_estimate,
    variance,
)
from evals.runner.live import LiveSettings
from evals.runner.run import EvalError, run_evals
from tests.evalcases import SYNTHETIC, write_case
from tests.llmcases import make_request

COUNTER = CharTokenCounter()
PRICE = CostModel(input_per_mtok=Decimal("2"), output_per_mtok=Decimal("10"))


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_the_estimator_counts_the_prompt_and_refuses_the_call() -> None:
    estimator = EstimatingLLM(COUNTER)

    with pytest.raises(LLMError):
        await estimator.diagnose(make_request())
    with pytest.raises(LLMError):
        await estimator.diagnose(make_request())

    assert estimator.calls == 2
    assert estimator.input_tokens > 0
    assert estimator.input_tokens % 2 == 0  # the same prompt twice, up to the nonce length


def test_an_empty_cache_is_all_calls_and_the_estimate_follows_the_prices(
    cases_dir: Path, tmp_path: Path
) -> None:
    pairs = build_estimating_setups(
        ["m-a"],
        COUNTER,
        cache_dir=tmp_path / "answers",
        prices={"m-a": PRICE},
        settings=LiveSettings(),
    )

    run_evals("llm", ["lab"], cases_dir, COUNTER, llms=[s for s, _ in pairs])

    setup, estimator = pairs[0]
    estimate = estimate_of(setup, estimator, assumed_output_tokens=300)
    assert estimate.calls == estimator.calls == len(SYNTHETIC)
    assert estimate.input_tokens == estimator.input_tokens > 0
    expected = PRICE.cost(estimate.input_tokens, estimate.calls * 300)
    assert estimate.expected_usd == expected
    assert estimate.ceiling_usd > estimate.expected_usd
    assert estimate.priced


def test_a_filled_cache_is_free(cases_dir: Path, tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    fill = CachedLLMClient(FakeLLM([valid()] * 50), answers, model="m-a")
    from evals.runner.run import LLMSetup

    run_evals(
        "llm", ["lab"], cases_dir, COUNTER, llms=[LLMSetup(client=fill, label="m-a", cost=PRICE)]
    )
    pairs = build_estimating_setups(
        ["m-a"], COUNTER, cache_dir=answers, prices={"m-a": PRICE}, settings=LiveSettings()
    )

    run_evals("llm", ["lab"], cases_dir, COUNTER, llms=[s for s, _ in pairs])

    estimate = estimate_of(*pairs[0], assumed_output_tokens=300)
    assert (estimate.calls, estimate.input_tokens, estimate.expected_usd) == (0, 0, Decimal(0))


def test_the_estimate_says_when_the_caps_would_stop_a_live_run() -> None:
    one = Estimate(
        "m",
        calls=100,
        input_tokens=200_000,
        expected_usd=Decimal("0.5"),
        ceiling_usd=Decimal("1.2"),
        priced=True,
    )

    fits, over = render_estimate(
        [one], max_cost_usd=Decimal("1.5"), max_calls=150, assumed_output_tokens=300
    )
    assert not over
    assert "fits" in fits
    assert "it would not" not in fits  # 1.2 fits the cap: no warning about the ceiling

    _, too_dear = render_estimate(
        [one], max_cost_usd=Decimal("0.4"), max_calls=150, assumed_output_tokens=300
    )
    _, too_many = render_estimate(
        [one], max_cost_usd=Decimal("1.5"), max_calls=50, assumed_output_tokens=300
    )
    assert too_dear
    assert too_many

    note, over = render_estimate(
        [one], max_cost_usd=Decimal("1.0"), max_calls=150, assumed_output_tokens=300
    )
    assert not over
    assert "at the output ceiling it would not" in note


def test_an_unpriced_model_is_flagged_not_hidden() -> None:
    free = Estimate(
        "m",
        calls=3,
        input_tokens=9000,
        expected_usd=Decimal(0),
        ceiling_usd=Decimal(0),
        priced=False,
    )

    text, _ = render_estimate(
        [free], max_cost_usd=Decimal("1"), max_calls=10, assumed_output_tokens=300
    )

    assert "no price" in text


def test_the_cli_refuses_live_with_estimate_and_the_rules_variant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "--estimate", "--live", "--variant", "llm", "--model", "m"]) == 1
    assert "opposites" in capsys.readouterr().err
    assert cli.main(["run", "--estimate", "--variant", "rules"]) == 1
    assert "llm variant" in capsys.readouterr().err


def test_the_cli_estimate_exits_5_when_the_caps_would_stop_the_run(
    cases_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "run",
        "--estimate",
        "--variant",
        "llm",
        "--model",
        "m-a",
        "--price",
        "m-a=2,10",
        "--cases-dir",
        str(cases_dir),
        "--cache-dir",
        str(tmp_path / "answers"),
    ]

    assert cli.main([*args, "--max-calls", "1"]) == cli.EXIT_BUDGET
    assert "STOP" in capsys.readouterr().out
    assert cli.main([*args, "--max-calls", "100", "--max-cost-usd", "5"]) == 0
    assert "fits" in capsys.readouterr().out


# --- variance -----------------------------------------------------------------------------------


def report(path: Path, **macro_f1: float) -> Path:
    data = {
        "variants": {
            name: {"metrics": {"cases": 52, "macro_f1": value}, "cases": []}
            for name, value in macro_f1.items()
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_variance_gives_mean_sigma_and_range(tmp_path: Path) -> None:
    paths = [
        report(tmp_path / f"r{i}.json", rules=0.6, **{"llm+hints [m]": v})
        for i, v in enumerate((0.80, 0.82, 0.84))
    ]

    [spread] = variance(paths)

    assert spread.variant == "llm+hints [m]"  # the rules variant has no noise and is skipped
    assert spread.reports == 3
    assert spread.cases == 52
    assert spread.mean == pytest.approx(0.82)
    assert spread.sigma == pytest.approx(0.02)
    assert (spread.low, spread.high) == (0.80, 0.84)


def test_variance_can_name_a_variant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = report(tmp_path / "a.json", **{"llm [m]": 0.5, "llm+hints [m]": 0.7})
    b = report(tmp_path / "b.json", **{"llm [m]": 0.6, "llm+hints [m]": 0.7})

    assert cli.main(["variance", str(a), str(b), "--variant", "llm+hints [m]"]) == 0

    out = capsys.readouterr().out
    assert "llm+hints [m]" in out
    assert "sigma 0.000" in out
    assert "llm [m]:" not in out


def test_variance_refuses_what_it_cannot_compare(tmp_path: Path) -> None:
    a = report(tmp_path / "a.json", **{"llm [m]": 0.5})
    with pytest.raises(EvalError, match="two reports"):
        variance([a])
    b = report(tmp_path / "b.json", **{"llm+hints [m]": 0.5})
    with pytest.raises(EvalError, match="not in every report"):
        variance([a, b])
    junk = tmp_path / "junk.json"
    junk.write_text("not json", encoding="utf-8")
    with pytest.raises(EvalError, match="not a report"):
        variance([a, junk])
    only_rules = [report(tmp_path / f"o{i}.json", rules=0.6) for i in range(2)]
    with pytest.raises(EvalError, match="no variant"):
        variance(only_rules)
