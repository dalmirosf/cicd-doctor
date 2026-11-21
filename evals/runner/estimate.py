"""What a live run would cost before it runs, and how noisy the model is (P4 (h), D-112).

Evals only: nothing here is in ``core/`` and nothing calls a model.

``run --estimate`` runs the variants offline over the same cases with an ``EstimatingLLM`` behind
the answer cache. A cached answer is free, as in a live run. A miss would be a call: the estimator
counts the tokens of the prompt exactly as the OpenAI client would send it and refuses the call, so
the pipeline falls back to the rules and goes on. The output tokens are a guess (the real ones
are only known after the call), so the estimate shows the expected cost and the ceiling at
``LLM_MAX_OUTPUT_TOKENS``.

``variance`` reads two or more reports of the same cases and gives the mean and the standard
deviation of the macro-F1, to compare the model's noise with the 0.05 of the gate (D-105).
"""

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import build_messages, new_nonce
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from cicd_doctor.core.ports import TokenCounter
from evals.runner.live import LiveSettings
from evals.runner.recording import RecordingLLM
from evals.runner.run import EvalError, LLMSetup

DEFAULT_ASSUMED_OUTPUT_TOKENS = 300
THRESHOLD = 0.05  # the macro-F1 drop that fails the gate (D-105)


class EstimatingLLM:
    """Implements ``LLMClient``: counts what a call would send and never answers."""

    def __init__(self, counter: TokenCounter) -> None:
        self._counter = counter
        self.calls = 0
        self.input_tokens = 0

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        messages = build_messages(request, new_nonce())
        self.calls += 1
        self.input_tokens += self._counter.count(messages.system) + self._counter.count(
            messages.user
        )
        raise LLMError("estimate only: no model is called")


@dataclass(frozen=True)
class Estimate:
    model: str
    calls: int
    input_tokens: int
    expected_usd: Decimal
    ceiling_usd: Decimal
    priced: bool


def build_estimating_setups(
    models: Sequence[str],
    counter: TokenCounter,
    *,
    cache_dir: Path,
    prices: Mapping[str, CostModel],
    settings: LiveSettings,
) -> list[tuple[LLMSetup, EstimatingLLM]]:
    """One setup per model, reading the answer cache and counting the misses."""
    default_cost = CostModel(
        input_per_mtok=settings.llm_price_input_per_mtok or Decimal(0),
        output_per_mtok=settings.llm_price_output_per_mtok or Decimal(0),
    )
    setups = []
    for model in models:
        estimator = EstimatingLLM(counter)
        cache = CachedLLMClient(estimator, cache_dir, model=model)
        recorder = RecordingLLM(cache)
        cost = prices.get(model, default_cost)
        setup = LLMSetup(
            client=recorder,
            label=model,
            cost=cost,
            max_output_tokens=settings.llm_max_output_tokens,
            recorder=recorder,
            cache=cache,
        )
        setups.append((setup, estimator))
    return setups


def estimate_of(setup: LLMSetup, estimator: EstimatingLLM, assumed_output_tokens: int) -> Estimate:
    cost = setup.cost
    return Estimate(
        model=setup.label,
        calls=estimator.calls,
        input_tokens=estimator.input_tokens,
        expected_usd=cost.cost(estimator.input_tokens, estimator.calls * assumed_output_tokens),
        ceiling_usd=cost.cost(estimator.input_tokens, estimator.calls * setup.max_output_tokens),
        priced=cost.input_per_mtok > 0 or cost.output_per_mtok > 0,
    )


def render_estimate(
    estimates: Sequence[Estimate],
    *,
    max_cost_usd: Decimal,
    max_calls: int,
    assumed_output_tokens: int,
) -> tuple[str, bool]:
    """The text to print and whether a live run with these caps would stop before the end."""
    lines = []
    calls = sum(e.calls for e in estimates)
    expected = sum((e.expected_usd for e in estimates), Decimal(0))
    ceiling = sum((e.ceiling_usd for e in estimates), Decimal(0))
    for e in estimates:
        lines.append(
            f"{e.model}: {e.calls} calls without a cached answer, {e.input_tokens} input tokens, "
            f"about ${e.expected_usd} (at most ${e.ceiling_usd})"
        )
        if not e.priced:
            lines.append(f"{e.model}: no price (--price or LLM_PRICE_*): the cost shows as 0")
    lines.append(
        f"total: {calls} calls, about ${expected} (at most ${ceiling}), counting "
        f"{assumed_output_tokens} output tokens per call"
    )
    over = calls > max_calls or expected > max_cost_usd
    lines.append(
        f"caps: --max-calls {max_calls}, --max-cost-usd {max_cost_usd}: "
        + ("a live run would STOP before the end" if over else "a live run fits")
    )
    if not over and ceiling > max_cost_usd:
        lines.append("note: it fits on the expected cost; at the output ceiling it would not")
    lines.append(
        "approximate: a repair call, a retry or a different 'similar' history is not counted"
    )
    return "\n".join(lines), over


# --- variance -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Spread:
    variant: str
    reports: int
    cases: int
    mean: float
    sigma: float
    low: float
    high: float


def _metrics(path: Path) -> dict[str, tuple[int, float]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        variants: Mapping[str, Any] = data["variants"]
        return {
            name: (int(v["metrics"]["cases"]), float(v["metrics"]["macro_f1"]))
            for name, v in variants.items()
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvalError(f"{path}: not a report of this runner ({type(exc).__name__})") from None


def variance(paths: Sequence[Path], wanted: Sequence[str] = ()) -> list[Spread]:
    """The spread of the macro-F1 of each variant across ``paths`` (two reports at least)."""
    if len(paths) < 2:
        raise EvalError("variance needs two reports at least")
    reports = [_metrics(p) for p in paths]
    names = [n for n in reports[0] if (not wanted or n in wanted) and n != "rules"]
    if not names:
        raise EvalError("no variant to compare (the rules variant has no noise)")
    spreads = []
    for name in names:
        rows = [r.get(name) for r in reports]
        if any(row is None for row in rows):
            raise EvalError(f"variant {name!r} is not in every report")
        found = [row for row in rows if row is not None]
        cases = {row[0] for row in found}
        if len(cases) != 1:
            raise EvalError(f"variant {name!r}: the reports have different numbers of cases")
        values = [row[1] for row in found]
        spreads.append(
            Spread(
                variant=name,
                reports=len(values),
                cases=cases.pop(),
                mean=statistics.fmean(values),
                sigma=statistics.stdev(values),
                low=min(values),
                high=max(values),
            )
        )
    return spreads


def render_variance(spreads: Sequence[Spread]) -> str:
    lines = []
    for s in spreads:
        verdict = "well under" if 2 * s.sigma < THRESHOLD else "NOT well under"
        lines.append(
            f"{s.variant}: {s.reports} reports of {s.cases} cases, macro-F1 mean {s.mean:.3f}, "
            f"sigma {s.sigma:.3f}, range {s.low:.3f} to {s.high:.3f}: two sigma is {verdict} "
            f"the gate threshold of {THRESHOLD}"
        )
    return "\n".join(lines)
