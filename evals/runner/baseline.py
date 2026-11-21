"""Reader of ``evals/baseline.json``, the last accepted metrics (SPEC §11.3, D-105).

This module only READS the file. The writer (``evals baseline --accept``) and the CI rule are PR
(g); the reports (verdict changes) and the README table (``readme_table.py``) depend on the shape
below, so it is documented here and checked by ``load_baseline``.

Minimal schema (extra keys, at any level, are accepted and ignored)::

    {
      "schema_version": 1,
      "variants": {
        "<variant name as in the report, e.g. rules or llm+hints [gpt-6-luna]>": {
          "model": "<model id>" | null,            # null for rules
          "cases": 55,                             # N the metrics were computed over
          "accuracy": 0.9,                         # 0..1
          "macro_f1": 0.88,                        # 0..1
          "cause_match_mean": 1.6 | null,          # mean judge score 0..2, null = not judged
          "cost_per_llm_diagnosis": 0.0004 | null, # USD, null = no LLM diagnosis
          "latency_ms_p50": 900 | null,            # model call, null = no model call
          "latency_ms_p95": 2100 | null,
          "prices": {"input_per_mtok": 0.1, "output_per_mtok": 0.4} | null,
          "verdicts": {"<case id>": "<predicted category>"}   # optional, feeds the report's
        }                                                     # "Verdict changes" section
      }
    }
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASELINE = Path(__file__).resolve().parents[1] / "baseline.json"
SUPPORTED_VERSIONS = (1,)


class BaselineError(ValueError):
    """The baseline file is not what the schema above says."""


@dataclass(frozen=True)
class Prices:
    input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class BaselineVariant:
    name: str
    model: str | None
    cases: int
    accuracy: float
    macro_f1: float
    cause_match_mean: float | None
    cost_per_llm_diagnosis: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    prices: Prices | None
    verdicts: dict[str, str] | None  # None: the baseline has no per-case verdicts


@dataclass(frozen=True)
class Baseline:
    variants: dict[str, BaselineVariant]


def load_baseline(path: Path = DEFAULT_BASELINE) -> Baseline | None:
    """The accepted baseline, or None when the file does not exist (nothing accepted yet)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path.name} is not valid JSON: {exc.msg}") from None
    return parse_baseline(data)


def parse_baseline(data: object) -> Baseline:
    if not isinstance(data, dict):
        raise BaselineError("the baseline must be a JSON object")
    if data.get("schema_version") not in SUPPORTED_VERSIONS:
        raise BaselineError(f"unsupported baseline schema_version {data.get('schema_version')!r}")
    raw = data.get("variants")
    if not isinstance(raw, dict) or not raw:
        raise BaselineError("the baseline needs a non-empty 'variants' object")
    return Baseline({name: _variant(name, body) for name, body in raw.items()})


def _variant(name: str, body: Any) -> BaselineVariant:
    if not isinstance(body, dict):
        raise BaselineError(f"variant {name!r} must be an object")
    prices = body.get("prices")
    verdicts = body.get("verdicts")
    if verdicts is not None and not (
        isinstance(verdicts, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in verdicts.items())
    ):
        raise BaselineError(f"variant {name!r}: 'verdicts' must map case ids to categories")
    return BaselineVariant(
        name=name,
        model=_optional(name, body, "model", str),
        cases=_required_int(name, body, "cases"),
        accuracy=_required_share(name, body, "accuracy"),
        macro_f1=_required_share(name, body, "macro_f1"),
        cause_match_mean=_optional(name, body, "cause_match_mean", float),
        cost_per_llm_diagnosis=_optional(name, body, "cost_per_llm_diagnosis", float),
        latency_ms_p50=_optional(name, body, "latency_ms_p50", float),
        latency_ms_p95=_optional(name, body, "latency_ms_p95", float),
        prices=_prices(name, prices),
        verdicts=verdicts,
    )


def _prices(name: str, raw: object) -> Prices | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BaselineError(f"variant {name!r}: 'prices' must be an object or null")
    return Prices(
        _required_number(name, raw, "input_per_mtok"),
        _required_number(name, raw, "output_per_mtok"),
    )


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _required_number(name: str, body: dict[str, Any], key: str) -> float:
    if not _number(body.get(key)):
        raise BaselineError(f"variant {name!r}: {key!r} must be a number")
    return float(body[key])


def _required_share(name: str, body: dict[str, Any], key: str) -> float:
    value = _required_number(name, body, key)
    if not 0 <= value <= 1:
        raise BaselineError(f"variant {name!r}: {key!r} must be between 0 and 1")
    return value


def _required_int(name: str, body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BaselineError(f"variant {name!r}: {key!r} must be a non-negative integer")
    return value


def _optional(name: str, body: dict[str, Any], key: str, kind: type) -> Any:
    value = body.get(key)
    if value is None:
        return None
    if kind is float and _number(value):
        return float(value)
    if kind is str and isinstance(value, str):
        return value
    raise BaselineError(f"variant {name!r}: {key!r} must be {kind.__name__} or null")
