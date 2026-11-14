"""Metrics of one variant over its case results (SPEC §11.2; P1b subset, no judge yet)."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from cicd_doctor.core.models import Category
from evals.runner.variants import CaseResult

HIGH_CONFIDENCE = 0.75  # SPEC §5.1 confidence buckets
MEDIUM_CONFIDENCE = 0.45
LABELS = [category.value for category in Category]


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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def bucket(confidence: float) -> str:
    if confidence >= HIGH_CONFIDENCE:
        return "high"
    return "medium" if confidence >= MEDIUM_CONFIDENCE else "low"


def compute(results: Sequence[CaseResult]) -> Metrics:
    n = len(results)
    per_category = _per_category(results)
    supported = [score.f1 for score in per_category.values() if score.support]
    abstaining = [
        r for r in results if r.predicted == Category.UNKNOWN.value or bucket(r.confidence) == "low"
    ]
    latencies = sorted(result.latency_ms for result in results)
    return Metrics(
        cases=n,
        errors=sum(result.error is not None for result in results),
        accuracy=_share(sum(r.correct for r in results), n),
        macro_f1=round(sum(supported) / len(supported), 4) if supported else 0.0,
        per_category=per_category,
        confusion=_confusion(results),
        evidence_in_excerpt=_evidence_share(results, lambda r: r.excerpt_lines),
        evidence_in_diagnosis=_evidence_share(results, lambda r: r.evidence_lines),
        abstention=_share(len(abstaining), n),
        calibration=_calibration(results),
        latency_ms_p50=_percentile(latencies, 0.50),
        latency_ms_p95=_percentile(latencies, 0.95),
        cost_usd_total=round(sum(result.cost_usd for result in results), 6),
    )


def _confusion(results: Sequence[CaseResult]) -> dict[str, dict[str, int]]:
    cells = Counter((result.expected, result.predicted) for result in results)
    confusion: dict[str, dict[str, int]] = {}
    for (expected, predicted), count in sorted(cells.items()):
        confusion.setdefault(expected, {})[predicted] = count
    return confusion


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
