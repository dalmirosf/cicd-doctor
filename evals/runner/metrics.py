"""Metrics of one variant over its case results (SPEC §11.2; P1b subset, no judge yet)."""

import math
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from cicd_doctor.core.models import Category
from evals.runner.variants import CaseResult

HIGH_CONFIDENCE = 0.75  # SPEC §5.1 confidence buckets
MEDIUM_CONFIDENCE = 0.45
LABELS = [category.value for category in Category]
SCHEMA_VERSION = 2  # report JSON: 2 adds the P4 (b) metrics, every v1 field keeps its name (D-109)
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261120  # fixed: the same results always give the same interval (D-105)
MIN_DECILE_CASES = 5  # a decile with fewer cases is not shown (it is noise)
INTERVAL_GROUPS = {
    "lab": ("lab",),
    "public": ("public",),
    "injection": ("injection",),
    "lab+public": ("lab", "public"),
}


@dataclass(frozen=True)
class CategoryScore:
    precision: float
    recall: float
    f1: float
    support: int  # cases whose expected category is this one
    predicted: int  # cases predicted as this category


@dataclass(frozen=True)
class Metrics:
    cases: int
    errors: int
    accuracy: float
    macro_f1: float  # mean F1 over the categories that have support
    per_category: dict[str, CategoryScore]
    confusion: dict[str, dict[str, int]]  # expected -> predicted -> count (non-zero cells)
    evidence_in_excerpt: float | None  # share of expected evidence lines kept in the excerpt
    evidence_in_diagnosis: float | None  # share of expected evidence lines cited as evidence
    abstention: float  # share of unknown or low-confidence answers
    calibration: dict[str, dict[str, float]]  # bucket -> {cases, accuracy}
    latency_ms_p50: float
    latency_ms_p95: float
    cost_usd_total: float
    cost_usd_per_case: float
    fallbacks: dict[str, int]  # fallback_reason -> cases that published the rules diagnosis
    llm_share: float  # share of cases whose published diagnosis came from the LLM
    llm_calls: int
    input_tokens: int
    output_tokens: int
    attack_cases: int  # injection cases among the results
    attacks_won: int  # of those, the ones the attacker won (any way, see ``injection.judge``)
    attack_success_rate: float | None  # None without injection cases; the target is 0
    # Only ``llm+hints+similar`` has a history (D-86); None for the other variants.
    cache_hits: int | None  # cases whose published diagnosis was served from the cache
    cache_hit_rate: float | None  # ... over the cases that have a history (not injection)
    similar_hits: int | None  # cases that had at least one similar past failure
    similar_hit_rate: float | None
    # --- P4 (b), D-109 ---------------------------------------------------------------------
    # Latency of the model call itself, from the recorded ``LLMResult.latency_ms``. ``latency_ms_*``
    # above stay the pipeline time of THIS run (with stored answers it is the read of a file).
    llm_latency_ms_p50: float | None
    llm_latency_ms_p95: float | None
    # Cost over the diagnoses published from the model (not cache, not fallback). ``priced`` says
    # whether the prices were known: with none, every cost is 0 and the report says so.
    priced: bool | None
    llm_diagnoses: int
    cost_usd_per_llm_diagnosis: float | None  # NFR-03 average
    cost_usd_p95: float | None
    # Evidence: raw items of the answer that ``verified_evidence`` keeps, over all raw items.
    evidence_validity: float | None
    answers: int  # cases with a recorded answer of the model
    answers_without_valid_evidence: int
    abstention_unknown: float  # share of cases that say ``unknown``
    abstention_low_confidence: float  # share with confidence below 0.45
    calibration_deciles: list[dict[str, float]]  # deciles with >= 5 cases
    expected_calibration_error: float | None
    high_confidence_accuracy: float | None
    intervals: dict[str, dict[str, object]]  # bootstrap 95 % CI per source group (informational)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def bucket(confidence: float) -> str:
    if confidence >= HIGH_CONFIDENCE:
        return "high"
    return "medium" if confidence >= MEDIUM_CONFIDENCE else "low"


def compute(
    results: Sequence[CaseResult], *, with_history: bool = False, priced: bool | None = None
) -> Metrics:
    n = len(results)
    per_category = _per_category(results)
    abstaining = [
        r for r in results if r.predicted == Category.UNKNOWN.value or bucket(r.confidence) == "low"
    ]
    latencies = sorted(result.latency_ms for result in results)
    attacks = [result for result in results if result.attack]
    remembered = [result for result in results if result.source != "injection"]
    cache_hits = sum(r.diagnosis_source == "cache" for r in remembered)
    similar_hits = sum(r.similar_found > 0 for r in remembered)
    llm_latencies = sorted(r.llm_latency_ms for r in results if r.llm_latency_ms is not None)
    from_model = [r for r in results if r.diagnosis_source == "llm"]
    model_costs = sorted(r.cost_usd for r in from_model)
    answered = [r for r in results if r.llm_latency_ms is not None]
    raw_items = sum(r.answer_raw_items for r in answered)
    deciles, ece = _deciles(results)
    high = [r for r in results if r.error is None and bucket(r.confidence) == "high"]
    return Metrics(
        cases=n,
        errors=sum(result.error is not None for result in results),
        accuracy=_share(sum(r.correct for r in results), n),
        macro_f1=macro_f1(results),
        per_category=per_category,
        confusion=_confusion(results),
        evidence_in_excerpt=_evidence_share(results, lambda r: r.excerpt_lines),
        evidence_in_diagnosis=_evidence_share(results, lambda r: r.evidence_lines),
        abstention=_share(len(abstaining), n),
        calibration=_calibration(results),
        latency_ms_p50=_percentile(latencies, 0.50),
        latency_ms_p95=_percentile(latencies, 0.95),
        cost_usd_total=round(sum(result.cost_usd for result in results), 6),
        cost_usd_per_case=round(sum(r.cost_usd for r in results) / n, 6) if n else 0.0,
        fallbacks=dict(Counter(r.fallback_reason for r in results if r.fallback_reason)),
        llm_share=_share(sum(r.diagnosis_source == "llm" for r in results), n),
        llm_calls=sum(r.llm_calls for r in results),
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        attack_cases=len(attacks),
        attacks_won=sum(bool(r.wins) for r in attacks),
        attack_success_rate=_share(sum(bool(r.wins) for r in attacks), len(attacks))
        if attacks
        else None,
        cache_hits=cache_hits if with_history else None,
        cache_hit_rate=_share(cache_hits, len(remembered)) if with_history else None,
        similar_hits=similar_hits if with_history else None,
        similar_hit_rate=_share(similar_hits, len(remembered)) if with_history else None,
        llm_latency_ms_p50=_percentile(llm_latencies, 0.50) if llm_latencies else None,
        llm_latency_ms_p95=_percentile(llm_latencies, 0.95) if llm_latencies else None,
        priced=priced,
        llm_diagnoses=len(from_model),
        cost_usd_per_llm_diagnosis=(
            round(sum(model_costs) / len(model_costs), 6) if model_costs else None
        ),
        cost_usd_p95=_percentile(model_costs, 0.95) if model_costs else None,
        evidence_validity=(
            _share(sum(r.answer_valid_items for r in answered), raw_items) if raw_items else None
        ),
        answers=len(answered),
        answers_without_valid_evidence=sum(r.answer_valid_items == 0 for r in answered),
        abstention_unknown=_share(
            sum(r.error is None and r.predicted == Category.UNKNOWN.value for r in results), n
        ),
        abstention_low_confidence=_share(
            sum(r.error is None and bucket(r.confidence) == "low" for r in results), n
        ),
        calibration_deciles=deciles,
        expected_calibration_error=ece,
        high_confidence_accuracy=(
            _share(sum(r.correct for r in high), len(high)) if high else None
        ),
        intervals=bootstrap_intervals(results),
    )


def _confusion(results: Sequence[CaseResult]) -> dict[str, dict[str, int]]:
    cells = Counter((result.expected, result.predicted) for result in results)
    confusion: dict[str, dict[str, int]] = {}
    for (expected, predicted), count in sorted(cells.items()):
        confusion.setdefault(expected, {})[predicted] = count
    return confusion


def macro_f1(results: Sequence[CaseResult]) -> float:
    supported = [score.f1 for score in _per_category(results).values() if score.support]
    return round(sum(supported) / len(supported), 4) if supported else 0.0


def bootstrap_intervals(
    results: Sequence[CaseResult],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, dict[str, object]]:
    """95 % percentile intervals of accuracy and macro-F1, per source group (informational).

    Each group resamples its cases with replacement ``resamples`` times from its own
    ``random.Random(seed)``, so the numbers do not depend on the order of the groups or of the
    process. A group with no cases is left out.
    """
    intervals: dict[str, dict[str, object]] = {}
    for name, sources in INTERVAL_GROUPS.items():
        members = [r for r in results if r.source in sources]
        if not members:
            continue
        rng = random.Random(seed)
        accuracies: list[float] = []
        f1s: list[float] = []
        for _ in range(resamples):
            sample = rng.choices(members, k=len(members))
            accuracies.append(sum(r.correct for r in sample) / len(sample))
            f1s.append(macro_f1(sample))
        intervals[name] = {
            "cases": len(members),
            "resamples": resamples,
            "accuracy": _interval(accuracies),
            "macro_f1": _interval(f1s),
        }
    return intervals


def _interval(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    last = len(ordered) - 1
    return {
        "low": round(ordered[math.floor(0.025 * last)], 4),
        "high": round(ordered[math.ceil(0.975 * last)], 4),
    }


def _deciles(results: Sequence[CaseResult]) -> tuple[list[dict[str, float]], float | None]:
    """Accuracy per confidence decile (shown from 5 cases) and the expected calibration error.

    The error weights every decile by its cases, small ones included: it is the standard ECE
    over the answers that did not error.
    """
    bins: dict[int, list[CaseResult]] = {}
    for r in results:
        if r.error is None:
            bins.setdefault(min(int(round(r.confidence * 10, 9)), 9), []).append(r)
    total = sum(len(members) for members in bins.values())
    if not total:
        return [], None
    table: list[dict[str, float]] = []
    error = 0.0
    for index in sorted(bins):
        members = bins[index]
        accuracy = sum(r.correct for r in members) / len(members)
        confidence = sum(r.confidence for r in members) / len(members)
        error += len(members) / total * abs(accuracy - confidence)
        if len(members) >= MIN_DECILE_CASES:
            table.append(
                {
                    "from": index / 10,
                    "to": (index + 1) / 10,
                    "cases": len(members),
                    "accuracy": round(accuracy, 4),
                    "mean_confidence": round(confidence, 4),
                }
            )
    return table, round(error, 4)


def _per_category(results: Sequence[CaseResult]) -> dict[str, CategoryScore]:
    expected = Counter(result.expected for result in results)
    predicted = Counter(result.predicted for result in results)
    hits = Counter(result.expected for result in results if result.correct)
    scores: dict[str, CategoryScore] = {}
    for label in LABELS:
        if not expected[label] and not predicted[label]:
            continue
        precision = hits[label] / predicted[label] if predicted[label] else 0.0
        recall = hits[label] / expected[label] if expected[label] else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        scores[label] = CategoryScore(
            round(precision, 4), round(recall, 4), round(f1, 4), expected[label], predicted[label]
        )
    return scores


def _calibration(results: Sequence[CaseResult]) -> dict[str, dict[str, float]]:
    calibration: dict[str, dict[str, float]] = {}
    for name in ("high", "medium", "low"):
        members = [r for r in results if r.error is None and bucket(r.confidence) == name]
        calibration[name] = {
            "cases": len(members),
            "accuracy": _share(sum(r.correct for r in members), len(members)),
        }
    return calibration


def _evidence_share(
    results: Sequence[CaseResult], kept: Callable[[CaseResult], Sequence[int]]
) -> float | None:
    expected = found = 0
    for result in results:
        lines = set(kept(result))
        expected += len(result.expected_evidence)
        found += sum(line in lines for line in result.expected_evidence)
    return _share(found, expected) if expected else None


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; 0 for no values."""
    if not sorted_values:
        return 0.0
    rank = max(1, round(q * len(sorted_values) + 0.5))
    return sorted_values[min(rank, len(sorted_values)) - 1]
