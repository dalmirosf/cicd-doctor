"""Eval reports: ``evals/reports/<date>-<git-sha>.{json,md}`` (SPEC §11.3).

The Markdown report shows case ids, categories and rule ids only: never log text, which is
untrusted and could carry mentions or links.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "git_sha": self.git_sha,
            "sources": self.sources,
            "skipped_variants": self.skipped,
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
        "| Abstention | p95 latency (ms) | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in report.runs:
        m = run.metrics
        lines.append(
            f"| {run.variant} | {m.cases} | {_pct(m.accuracy)} | {m.macro_f1:.3f} "
            f"| {_pct(m.evidence_in_excerpt)} | {_pct(m.evidence_in_diagnosis)} "
            f"| {_pct(m.abstention)} | {m.latency_ms_p95:.1f} | {m.cost_usd_total:.4f} |"
        )
    for variant, reason in report.skipped.items():
        lines.append(f"| {variant} | not run: {reason} | | | | | | | |")
    for run in report.runs:
        lines += ["", f"## {run.variant}", "", *_per_category(run.metrics), ""]
        lines += [*_confusion(run.metrics.confusion), ""]
        lines += [*_calibration(run.metrics.calibration), ""]
        lines += _cases(run.results)
    return "\n".join(lines) + "\n"


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
        "| Case | Expected | Predicted | Confidence | Rules | OK |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        rules = ", ".join(r.rule_ids[:3]) or "-"
        verdict = "error: " + r.error if r.error else ("yes" if r.correct else "no")
        rows.append(
            f"| {r.case_id} | {r.expected} | {r.predicted} | {r.confidence:.2f} "
            f"| {rules} | {verdict} |"
        )
    return rows


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"
