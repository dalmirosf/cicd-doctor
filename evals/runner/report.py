"""Eval reports: ``evals/reports/<date>-<git-sha>.{json,md}`` (SPEC §11.3).

The Markdown report shows case ids, categories and rule ids only: never log text, which is
untrusted and could carry mentions or links.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from evals.runner.judge import CauseMatch, cause_match_lines
from evals.runner.metrics import Metrics
from evals.runner.variants import CaseResult

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


@dataclass(frozen=True)
class VariantRun:
    variant: str
    metrics: Metrics
    results: list[CaseResult]


@dataclass(frozen=True)
class Report:
    generated_at: datetime
    git_sha: str
    sources: list[str]
    runs: list[VariantRun]
    skipped: dict[str, str] = field(default_factory=dict)  # variant -> why it did not run
    cause_match: CauseMatch | None = None  # None: not judged (no ``--judge``, D-103)

    def to_dict(self) -> dict[str, object]:
        return {
            "cause_match": self.cause_match.to_dict() if self.cause_match else None,
            "generated_at": self.generated_at.isoformat(),
            "git_sha": self.git_sha,
            "sources": self.sources,
            "skipped_variants": self.skipped,
            "memory_effect": [asdict(effect) for effect in memory_effects(self)],
            "variants": {
                run.variant: {
                    "metrics": run.metrics.to_dict(),
                    "cases": [result.to_dict() for result in run.results],
                }
                for run in self.runs
            },
        }


def write_report(report: Report, out_dir: Path = DEFAULT_REPORTS_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report.generated_at:%Y-%m-%d}-{report.git_sha}"
    json_path, md_path = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path


def render_markdown(report: Report) -> str:
    lines = [
        f"# Eval report {report.generated_at:%Y-%m-%d} ({report.git_sha})",
        "",
        f"Sources: {', '.join(report.sources)}.",
        "",
        "| Variant | Cases | Accuracy | Macro-F1 | Evidence in excerpt | Evidence cited "
        "| Abstention | LLM share | Cache hits | Similar hits | Fallbacks | Attacks won "
        "| p50 / p95 latency (ms) | Cost (USD) | Cost per case |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in report.runs:
        m = run.metrics
        attacks = "n/a" if m.attack_success_rate is None else f"{m.attacks_won}/{m.attack_cases}"
        lines.append(
            f"| {run.variant} | {m.cases} | {_pct(m.accuracy)} | {m.macro_f1:.3f} "
            f"| {_pct(m.evidence_in_excerpt)} | {_pct(m.evidence_in_diagnosis)} "
            f"| {_pct(m.abstention)} | {_pct(m.llm_share)} "
            f"| {_hits(m.cache_hits, m.cache_hit_rate)} "
            f"| {_hits(m.similar_hits, m.similar_hit_rate)} | {sum(m.fallbacks.values())} "
            f"| {attacks} | {m.latency_ms_p50:.0f} / {m.latency_ms_p95:.0f} "
            f"| {m.cost_usd_total:.4f} | {m.cost_usd_per_case:.5f} |"
        )
    for variant, reason in report.skipped.items():
        lines.append(f"| {variant} | not run: {reason} | | | | | | | | | | | | | |")
    lines += _memory_effect(report)
    lines += cause_match_lines(report.cause_match)
    for run in report.runs:
        lines += ["", f"## {run.variant}", "", *_per_category(run.metrics), ""]
        lines += [*_confusion(run.metrics.confusion), ""]
        lines += [*_calibration(run.metrics.calibration), ""]
        lines += _cases(run.results)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class MemoryEffect:
    """``llm+hints+similar`` against ``llm+hints`` for one model (D-86)."""

    model: str
    cases: int
    accuracy_delta: float  # percentage points, similar minus hints
    cost_delta_usd: float  # total, similar minus hints
    cache_hits: int
    similar_hits: int


def memory_effects(report: Report) -> list[MemoryEffect]:
    by_name = {run.variant: run for run in report.runs}
    effects = []
    for name, run in by_name.items():
        prefix = "llm+hints+similar ["
        base = by_name.get("llm+hints [" + name[len(prefix) :]) if name.startswith(prefix) else None
        if base is None:
            continue
        m, b = run.metrics, base.metrics
        effects.append(
            MemoryEffect(
                model=name[len(prefix) : -1],
                cases=m.cases,
                accuracy_delta=round((m.accuracy - b.accuracy) * 100, 1),
                cost_delta_usd=round(m.cost_usd_total - b.cost_usd_total, 6),
                cache_hits=m.cache_hits or 0,
                similar_hits=m.similar_hits or 0,
            )
        )
    return effects


def _memory_effect(report: Report) -> list[str]:
    effects = memory_effects(report)
    if not effects:
        return []
    lines = [
        "",
        "## Effect of the memory (`llm+hints+similar` minus `llm+hints`)",
        "",
        "Small n: the lab has 23 distinct failures, so the effect shows almost only on its "
        "repeated captures (py-assert, py-modnf). Read these as a mechanism check, not as a "
        "measurement.",
        "",
        "| Model | Cases | Accuracy (pp) | Cost (USD) | Cache hits | Similar hits |",
        "|---|---|---|---|---|---|",
    ]
    for e in effects:
        lines.append(
            f"| {e.model} | {e.cases} | {e.accuracy_delta:+.1f} | {e.cost_delta_usd:+.4f} "
            f"| {e.cache_hits} | {e.similar_hits} |"
        )
    return lines


def _hits(count: int | None, rate: float | None) -> str:
    return "n/a" if count is None else f"{count} ({_pct(rate)})"


def _per_category(metrics: Metrics) -> list[str]:
    rows = [
        "| Category | Precision | Recall | F1 | Support | Predicted |",
        "|---|---|---|---|---|---|",
    ]
    for label, s in metrics.per_category.items():
        rows.append(
            f"| {label} | {s.precision:.3f} | {s.recall:.3f} | {s.f1:.3f} "
            f"| {s.support} | {s.predicted} |"
        )
    return rows


def _confusion(confusion: Mapping[str, Mapping[str, int]]) -> list[str]:
    predicted = sorted({p for row in confusion.values() for p in row})
    rows = [
        "Confusion matrix (rows: expected, columns: predicted):",
        "",
        "| expected \\ predicted | " + " | ".join(predicted) + " |",
        "|---|" + "---|" * len(predicted),
    ]
    for expected in sorted(confusion):
        cells = [str(confusion[expected].get(p, 0) or "") for p in predicted]
        rows.append(f"| {expected} | " + " | ".join(cells) + " |")
    return rows


def _calibration(calibration: Mapping[str, Mapping[str, float]]) -> list[str]:
    rows = ["| Confidence | Cases | Accuracy |", "|---|---|---|"]
    for name, values in calibration.items():
        rows.append(f"| {name} | {int(values['cases'])} | {_pct(values['accuracy'])} |")
    return rows


def _cases(results: Sequence[CaseResult]) -> list[str]:
    rows = [
        "| Case | Expected | Predicted | Confidence | Source | OK |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        source = ", ".join(r.rule_ids[:3]) if r.diagnosis_source == "rules" else r.diagnosis_source
        if r.fallback_reason:
            source = f"{source or 'rules'} ({r.fallback_reason})"
        verdict = "error: " + r.error if r.error else ("yes" if r.correct else "no")
        if r.wins:
            verdict += " · ATTACK WON: " + ", ".join(r.wins)
        rows.append(
            f"| {r.case_id} | {r.expected} | {r.predicted} | {r.confidence:.2f} "
            f"| {source or '-'} | {verdict} |"
        )
    return rows


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"
