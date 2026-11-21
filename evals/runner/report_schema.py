"""The declared shape of the report JSON, ``schema_version: 2`` (SPEC §11.3, P4 (f)).

A JSON-Schema subset (``type``, ``required``, ``properties``, ``items``, ``additionalProperties``
as a schema for the values of a map, ``enum``) and a small validator for it, so a test can check
every report without a new dependency. PR (g) reads this format: a field is added, never renamed.

Top level
    schema_version        2
    generated_at, git_sha, sources, empty_sources (requested sources with no cases)
    skipped_variants      variant -> why it did not run
    cause_match           the judge section, or null without ``--judge``
    flaky_sequences       the simulated sequences, or null
    memory_effect         ``llm+hints+similar`` against ``llm+hints``, per model
    by_source             variant -> source -> {cases, errors, accuracy, macro_f1, ...}
    by_tag_group          variant -> ``rules_known`` | ``outside_rules`` -> the same numbers
    by_tag                variant -> tag -> the same numbers
    injection             variant -> {attack_cases, attacks_won, attack_success_rate, ...}
    verdict_changes       {baseline, reason, variants -> {compared, changes[], added[], removed[]}}
    variants              variant -> {metrics (every §11.2 metric), cases[] (one row per case)}
"""

from collections.abc import Mapping
from typing import Any

Schema = Mapping[str, Any]

_NUMBER: Schema = {"type": "number"}
_NULLABLE_NUMBER: Schema = {"type": ["number", "null"]}
_INT: Schema = {"type": "integer"}
_STR: Schema = {"type": "string"}
_STR_LIST: Schema = {"type": "array", "items": _STR}


def _obj(required: list[str], properties: Mapping[str, Schema]) -> Schema:
    return {"type": "object", "required": required, "properties": dict(properties)}


def _map(values: Schema) -> Schema:
    return {"type": "object", "additionalProperties": values}


SLICE: Schema = _obj(
    ["cases", "errors", "accuracy", "macro_f1", "abstention", "attacks_won", "cost_usd_total"],
    {
        "cases": _INT,
        "errors": _INT,
        "accuracy": _NUMBER,
        "macro_f1": _NUMBER,
        "abstention": _NUMBER,
        "attacks_won": _INT,
        "cost_usd_total": _NUMBER,
    },
)

METRICS: Schema = _obj(
    [
        "cases",
        "errors",
        "accuracy",
        "macro_f1",
        "per_category",
        "confusion",
        "evidence_in_excerpt",
        "evidence_in_diagnosis",
        "evidence_validity",
        "answers",
        "answers_without_valid_evidence",
        "abstention",
        "abstention_unknown",
        "abstention_low_confidence",
        "calibration",
        "calibration_deciles",
        "expected_calibration_error",
        "high_confidence_accuracy",
        "latency_ms_p50",
        "latency_ms_p95",
        "llm_latency_ms_p50",
        "llm_latency_ms_p95",
        "cost_usd_total",
        "cost_usd_per_case",
        "cost_usd_per_llm_diagnosis",
        "cost_usd_p95",
        "llm_diagnoses",
        "priced",
        "attack_cases",
        "attacks_won",
        "attack_success_rate",
        "intervals",
    ],
    {
        "cases": _INT,
        "errors": _INT,
        "accuracy": _NUMBER,
        "macro_f1": _NUMBER,
        "per_category": _map(
            _obj(
                ["precision", "recall", "f1", "support", "predicted"],
                {
                    "precision": _NUMBER,
                    "recall": _NUMBER,
                    "f1": _NUMBER,
                    "support": _INT,
                    "predicted": _INT,
                },
            )
        ),
        "confusion": _map(_map(_INT)),
        "evidence_in_excerpt": _NULLABLE_NUMBER,
        "evidence_in_diagnosis": _NULLABLE_NUMBER,
        "evidence_validity": _NULLABLE_NUMBER,
        "answers": _INT,
        "answers_without_valid_evidence": _INT,
        "abstention": _NUMBER,
        "abstention_unknown": _NUMBER,
        "abstention_low_confidence": _NUMBER,
        "calibration": _map(_obj(["cases", "accuracy"], {"cases": _NUMBER, "accuracy": _NUMBER})),
        "calibration_deciles": {
            "type": "array",
            "items": _obj(
                ["from", "to", "cases", "accuracy", "mean_confidence"],
                {
                    "from": _NUMBER,
                    "to": _NUMBER,
                    "cases": _NUMBER,
                    "accuracy": _NUMBER,
                    "mean_confidence": _NUMBER,
                },
            ),
        },
        "expected_calibration_error": _NULLABLE_NUMBER,
        "high_confidence_accuracy": _NULLABLE_NUMBER,
        "latency_ms_p50": _NUMBER,
        "latency_ms_p95": _NUMBER,
        "llm_latency_ms_p50": _NULLABLE_NUMBER,
        "llm_latency_ms_p95": _NULLABLE_NUMBER,
        "cost_usd_total": _NUMBER,
        "cost_usd_per_case": _NUMBER,
        "cost_usd_per_llm_diagnosis": _NULLABLE_NUMBER,
        "cost_usd_p95": _NULLABLE_NUMBER,
        "llm_diagnoses": _INT,
        "priced": {"type": ["boolean", "null"]},
        "attack_cases": _INT,
        "attacks_won": _INT,
        "attack_success_rate": _NULLABLE_NUMBER,
        "intervals": _map(
            _obj(
                ["cases", "resamples", "accuracy", "macro_f1"],
                {
                    "cases": _INT,
                    "resamples": _INT,
                    "accuracy": _obj(["low", "high"], {"low": _NUMBER, "high": _NUMBER}),
                    "macro_f1": _obj(["low", "high"], {"low": _NUMBER, "high": _NUMBER}),
                },
            )
        ),
    },
)

CASE_ROW: Schema = _obj(
    ["case_id", "source", "variant", "expected", "predicted", "confidence", "error", "wins"],
    {
        "case_id": _STR,
        "source": {"type": "string", "enum": ["lab", "public", "injection"]},
        "variant": _STR,
        "expected": _STR,
        "predicted": _STR,
        "confidence": _NUMBER,
        "error": {"type": ["string", "null"]},
        "wins": _STR_LIST,
    },
)

_BY = _map(_map(SLICE))

REPORT_SCHEMA: Schema = _obj(
    [
        "schema_version",
        "generated_at",
        "git_sha",
        "sources",
        "empty_sources",
        "skipped_variants",
        "cause_match",
        "flaky_sequences",
        "memory_effect",
        "by_source",
        "by_tag_group",
        "by_tag",
        "injection",
        "verdict_changes",
        "variants",
    ],
    {
        "schema_version": {"type": "integer", "enum": [2]},
        "generated_at": _STR,
        "git_sha": _STR,
        "sources": _STR_LIST,
        "empty_sources": _STR_LIST,
        "skipped_variants": _map(_STR),
        "cause_match": {
            "type": ["object", "null"],
            "required": ["judge_model", "prompt_version", "fingerprint", "stats", "judgments"],
        },
        "flaky_sequences": {
            "type": ["object", "null"],
            "required": [
                "sequences",
                "correct",
                "accuracy",
                "resolved_flaky",
                "resolved_by_commit",
                "by_scenario",
                "misses",
                "lab_sequences",
                "lab_correct",
            ],
        },
        "memory_effect": {"type": "array"},
        "by_source": _BY,
        "by_tag_group": _BY,
        "by_tag": _BY,
        "injection": _map(
            _obj(
                ["attack_cases", "attacks_won", "attack_success_rate", "won_cases", "ways"],
                {
                    "attack_cases": _INT,
                    "attacks_won": _INT,
                    "attack_success_rate": _NUMBER,
                    "won_cases": _STR_LIST,
                    "ways": _STR_LIST,
                },
            )
        ),
        "verdict_changes": _obj(
            ["baseline", "reason", "variants", "without_verdicts", "not_in_baseline"],
            {
                "baseline": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
                "variants": _map(
                    _obj(
                        ["compared", "changes", "added", "removed"],
                        {
                            "compared": _INT,
                            "changes": {
                                "type": "array",
                                "items": _obj(
                                    ["case_id", "from", "to", "expected"],
                                    {"case_id": _STR, "from": _STR, "to": _STR, "expected": _STR},
                                ),
                            },
                            "added": _STR_LIST,
                            "removed": _STR_LIST,
                        },
                    )
                ),
                "without_verdicts": _STR_LIST,
                "not_in_baseline": _STR_LIST,
            },
        ),
        "variants": _map(
            _obj(
                ["metrics", "cases"],
                {"metrics": METRICS, "cases": {"type": "array", "items": CASE_ROW}},
            )
        ),
    },
)

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _is(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(value: object, schema: Schema, path: str = "$") -> list[str]:
    """The problems of ``value`` against ``schema``, as ``path: what`` strings (empty = valid)."""
    declared = schema.get("type")
    names = [declared] if isinstance(declared, str) else list(declared or [])
    if names and not any(_is(value, name) for name in names):
        return [f"{path}: expected {'|'.join(names)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in {schema['enum']}"]
    problems: list[str] = []
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing {key!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in properties:
                problems += validate(item, properties[key], f"{path}.{key}")
            elif extra is not None:
                problems += validate(item, extra, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems += validate(item, schema["items"], f"{path}[{index}]")
    return problems
