"""Eval reports: ``evals/reports/<date>-<git-sha>.{json,md}`` (SPEC §11.3).

The Markdown report shows case ids, categories and rule ids only: never log text, which is
untrusted and could carry mentions or links. The JSON has ``schema_version: 2`` and is described by
``evals/runner/report_schema.py`` (a test validates every report against it).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from evals.runner.baseline import Baseline
from evals.runner.flaky import FlakyMetrics
from evals.runner.judge import CauseMatch, cause_match_lines
from evals.runner.metrics import SCHEMA_VERSION, Metrics, compute
from evals.runner.variants import CaseResult

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"

# Tags of the cases whose ecosystem the rules know (python, docker, terraform and aws have rules;
# the tools run inside a python or docker job). Any other case counts as "outside" them.
RULES_KNOWN_TAGS = frozenset(
    {"python", "pytest", "uv", "pip", "mypy", "ruff", "resolver", "docker", "terraform", "aws"}
)
NOT_ECOSYSTEM_TAGS = frozenset({"synthetic", "injection"})
GROUP_KNOWN, GROUP_OUTSIDE = "rules_known", "outside_rules"
MIN_TAG_CASES = 2  # a tag with fewer cases is in the JSON but not in the Markdown table

# SPEC §11.2, row by row: (metric, text that must appear in the Markdown, key in the JSON). A test
# checks that every report has every one of them (P4 (f)).
METRIC_ROWS = (
    ("Category accuracy, per-category P/R/F1, macro-F1", "Per category", "variants"),
    ("Confusion matrix", "Confusion matrix", "variants"),
    ("Cause match", "## Cause match", "cause_match"),
    ("Evidence validity and recall", "## Evidence and abstention", "variants"),
    ("Flaky detection", "## Flaky and fix resolution", "flaky_sequences"),
    ("Abstention", "## Evidence and abstention", "variants"),
    ("Calibration (high/medium/low, deciles, ECE)", "Calibration", "variants"),
    ("Cost", "## Cost and latency", "variants"),
    ("Latency", "## Cost and latency", "variants"),
    ("Intervals", "## Bootstrap intervals", "variants"),
    ("Injection resistance", "## Injection resistance", "injection"),
)


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
    flaky: FlakyMetrics | None = None  # the outcome state machine over simulated sequences
    case_tags: dict[str, list[str]] = field(default_factory=dict)  # case id -> tags
    baseline: Baseline | None = None  # the accepted baseline, None when there is none
    empty_sources: list[str] = field(default_factory=list)  # requested sources with no cases

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cause_match": self.cause_match.to_dict() if self.cause_match else None,
            "generated_at": self.generated_at.isoformat(),
            "git_sha": self.git_sha,
            "sources": self.sources,
            "skipped_variants": self.skipped,
            "empty_sources": self.empty_sources,
            "memory_effect": [asdict(effect) for effect in memory_effects(self)],
            "flaky_sequences": self.flaky.to_dict() if self.flaky else None,
            "by_source": _dump(by_source(self)),
            "by_tag_group": _dump(by_tag_group(self)),
            "by_tag": _dump(by_tag(self)),
            "injection": {v: asdict(row) for v, row in injection_resistance(self).items()},
            "verdict_changes": verdict_changes(self),
            "variants": {
                run.variant: {
                    "metrics": run.metrics.to_dict(),
                    "cases": [result.to_dict() for result in run.results],
                }
                for run in self.runs
            },
        }


# --- slices of the results: by source, by tag, injection, verdict changes -----------------------


@dataclass(frozen=True)
class Slice:
    """The headline numbers of a subset of one variant's results (a source, a tag)."""

    cases: int
    errors: int
    accuracy: float
    macro_f1: float
    abstention: float
    attacks_won: int
    cost_usd_total: float


def _slice(run: VariantRun, results: Sequence[CaseResult]) -> Slice:
    m = compute(results, with_history=run.metrics.cache_hits is not None, priced=run.metrics.priced)
    return Slice(
        m.cases, m.errors, m.accuracy, m.macro_f1, m.abstention, m.attacks_won, m.cost_usd_total
    )


def _dump(table: Mapping[str, Mapping[str, Slice]]) -> dict[str, object]:
    return {v: {k: asdict(s) for k, s in inner.items()} for v, inner in table.items()}


def by_source(report: Report) -> dict[str, dict[str, Slice]]:
    """Variant -> source -> numbers, one entry per source that has results."""
    out: dict[str, dict[str, Slice]] = {}
    for run in report.runs:
        sources = dict.fromkeys(r.source for r in run.results)
        out[run.variant] = {
            source: _slice(run, [r for r in run.results if r.source == source])
            for source in sources
        }
    return out


def ecosystem_group(tags: Sequence[str]) -> str:
    return GROUP_KNOWN if RULES_KNOWN_TAGS & set(tags) else GROUP_OUTSIDE


def by_tag_group(report: Report) -> dict[str, dict[str, Slice]]:
    """Variant -> group (ecosystems the rules know, or outside) -> numbers; injection left out."""
    out: dict[str, dict[str, Slice]] = {}
    for run in report.runs:
        groups: dict[str, list[CaseResult]] = {}
        for r in run.results:
            if r.source != "injection":
                group = ecosystem_group(report.case_tags.get(r.case_id, []))
                groups.setdefault(group, []).append(r)
        out[run.variant] = {name: _slice(run, members) for name, members in sorted(groups.items())}
    return out


def by_tag(report: Report) -> dict[str, dict[str, Slice]]:
    """Variant -> tag -> numbers, over the lab and public cases that carry the tag."""
    out: dict[str, dict[str, Slice]] = {}
    for run in report.runs:
        tags: dict[str, list[CaseResult]] = {}
        for r in run.results:
            if r.source == "injection":
                continue
            for tag in report.case_tags.get(r.case_id, []):
                if tag not in NOT_ECOSYSTEM_TAGS:
                    tags.setdefault(tag, []).append(r)
        out[run.variant] = {tag: _slice(run, members) for tag, members in sorted(tags.items())}
    return out


@dataclass(frozen=True)
class InjectionRow:
    attack_cases: int
    attacks_won: int
    attack_success_rate: float
    won_cases: list[str]  # ids of the cases the attacker won
    ways: list[str]  # how (the vocabulary of ``injection.judge``)


def injection_resistance(report: Report) -> dict[str, InjectionRow]:
    """Variant -> attacks tried and won (target: none won)."""
    out: dict[str, InjectionRow] = {}
    for run in report.runs:
        attacks = [r for r in run.results if r.attack]
        if not attacks:
            continue
        won = [r.case_id for r in attacks if r.wins]
        out[run.variant] = InjectionRow(
            len(attacks),
            len(won),
            round(len(won) / len(attacks), 4),
            won,
            sorted({way for r in attacks for way in r.wins}),
        )
    return out


def verdict_changes(report: Report) -> dict[str, object]:
    """Cases whose predicted category differs from the baseline's: ids and categories only.

    Works without a baseline (``baseline: false`` and the reason). The baseline keeps the verdicts
    per variant name; a variant without them, or one the baseline does not have, is listed.
    """
    if report.baseline is None:
        return {
            "baseline": False,
            "reason": "no accepted baseline (evals/baseline.json does not exist)",
            "variants": {},
            "without_verdicts": [],
            "not_in_baseline": [],
        }
    variants: dict[str, object] = {}
    without: list[str] = []
    absent: list[str] = []
    for run in report.runs:
        old = report.baseline.variants.get(run.variant)
        if old is None:
            absent.append(run.variant)
            continue
        if old.verdicts is None:
            without.append(run.variant)
            continue
        now = {r.case_id: r for r in run.results}
        both = sorted(old.verdicts.keys() & now.keys())
        variants[run.variant] = {
            "compared": len(both),
            "changes": [
                {
                    "case_id": case_id,
                    "from": old.verdicts[case_id],
                    "to": now[case_id].predicted,
                    "expected": now[case_id].expected,
                }
                for case_id in both
                if old.verdicts[case_id] != now[case_id].predicted
            ],
            "added": sorted(now.keys() - old.verdicts.keys()),
            "removed": sorted(old.verdicts.keys() - now.keys()),
        }
    return {
        "baseline": True,
        "reason": None,
        "variants": variants,
        "without_verdicts": without,
        "not_in_baseline": absent,
    }


# --- files ----------------------------------------------------------------------------------------


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
        *(
            [f"No cases yet for: {', '.join(report.empty_sources)}."]
            if report.empty_sources
            else []
        ),
        *_summary(report),
        *_verdict_changes(report),
        *cause_match_lines(report.cause_match),
        *_flaky(report),
        *_injection(report),
        *_by_source(report),
        *_by_tag(report),
        *_evidence_and_abstention(report),
        *_cost_and_latency(report),
        *_intervals(report),
        *_memory_effect(report),
    ]
    for run in report.runs:
        lines += ["", f"## Detail: {run.variant}", "", *_per_category(run.metrics), ""]
        lines += [*_confusion(run.metrics.confusion), ""]
        lines += [*_calibration(run.metrics.calibration, run.metrics.calibration_deciles), ""]
        lines += _cases(run.results)
    return "\n".join(lines) + "\n"


def _summary(report: Report) -> list[str]:
    lines = [
        "",
        "## Summary",
        "",
        "| Variant | Cases | Accuracy | Macro-F1 | Cause match | Cost per LLM diagnosis (USD) "
        "| Model call p50 / p95 (ms) | Attacks won |",
        "|---|---|---|---|---|---|---|---|",
    ]
    means = _cause_means(report.cause_match)
    for run in report.runs:
        m = run.metrics
        attacks = "n/a" if m.attack_success_rate is None else f"{m.attacks_won}/{m.attack_cases}"
        lines.append(
            f"| {run.variant} | {m.cases} | {_pct(m.accuracy)} | {m.macro_f1:.3f} "
            f"| {means.get(run.variant, 'n/a')} | {_cost_per_diagnosis(m)} "
            f"| {_model_latency(m)} | {attacks} |"
        )
    for variant, reason in report.skipped.items():
        lines.append(f"| {variant} | not run: {reason} | | | | | | |")
    return lines


def _cause_means(cause_match: CauseMatch | None) -> dict[str, str]:
    if cause_match is None:
        return {}
    out: dict[str, str] = {}
    for variant, by_src in cause_match.stats().items():
        mean = by_src["all"].mean
        out[variant] = "n/a" if mean is None else f"{mean:.2f}"
    return out


def _verdict_changes(report: Report) -> list[str]:
    changes = verdict_changes(report)
    lines = ["", "## Verdict changes against the baseline", ""]
    if not changes["baseline"]:
        return [*lines, f"No baseline to compare with: {changes['reason']}."]
    variants = changes["variants"]
    assert isinstance(variants, dict)
    if not variants:
        lines.append("No variant of this run has verdicts in the baseline.")
    for name, body in variants.items():
        lines += ["", f"### {name}", ""]
        lines.append(
            f"{body['compared']} cases compared; {len(body['changes'])} changed, "
            f"{len(body['added'])} new, {len(body['removed'])} removed."
        )
        if body["changes"]:
            lines += ["", "| Case | Was | Now | Expected |", "|---|---|---|---|"]
            lines += [
                f"| {c['case_id']} | {c['from']} | {c['to']} | {c['expected']} |"
                for c in body["changes"]
            ]
        for key in ("added", "removed"):
            if body[key]:
                lines += ["", f"{key.capitalize()}: " + ", ".join(body[key]) + "."]
    for key, text in (
        ("without_verdicts", "Baseline has no per-case verdicts for"),
        ("not_in_baseline", "Not in the baseline:"),
    ):
        names = changes[key]
        assert isinstance(names, list)
        if names:
            lines += ["", f"{text} {', '.join(names)}."]
    return lines


def _injection(report: Report) -> list[str]:
    lines = ["", "## Injection resistance", ""]
    table = injection_resistance(report)
    if not table:
        return [*lines, "No injection cases in this run (add `injection` to `--sources`)."]
    lines += [
        "Target: 0 attacks won.",
        "",
        "| Variant | Attack cases | Attacks won | Success rate | Cases won |",
        "|---|---|---|---|---|",
    ]
    for variant, row in table.items():
        won = ", ".join(row.won_cases) or "none"
        lines.append(
            f"| {variant} | {row.attack_cases} | {row.attacks_won} "
            f"| {_pct(row.attack_success_rate)} | {won} |"
        )
    return lines


def _by_source(report: Report) -> list[str]:
    table = by_source(report)
    lines = [
        "",
        "## By source",
        "",
        "| Variant | Source | Cases | Accuracy | Macro-F1 | Abstention |",
        "|---|---|---|---|---|---|",
    ]
    for variant, sources in table.items():
        for source, s in sources.items():
            lines.append(
                f"| {variant} | {source} | {s.cases} | {_pct(s.accuracy)} "
                f"| {s.macro_f1:.3f} | {_pct(s.abstention)} |"
            )
    return lines


def _by_tag(report: Report) -> list[str]:
    lines = [
        "",
        "## By ecosystem tag",
        "",
        "Lab and public cases only (the injection cases are synthetic). A case is "
        "'rules-known' when it has a tag of an ecosystem the rules cover "
        f"({', '.join(sorted(RULES_KNOWN_TAGS))}); every other case is 'outside'.",
        "",
        "| Variant | Group | Cases | Accuracy | Macro-F1 |",
        "|---|---|---|---|---|",
    ]
    for variant, groups in by_tag_group(report).items():
        for group, s in groups.items():
            label = "rules-known" if group == GROUP_KNOWN else "outside the rules"
            lines.append(
                f"| {variant} | {label} | {s.cases} | {_pct(s.accuracy)} | {s.macro_f1:.3f} |"
            )
    lines += [
        "",
        f"By tag (tags with {MIN_TAG_CASES} or more cases):",
        "",
        "| Variant | Tag | Cases | Accuracy |",
        "|---|---|---|---|",
    ]
    for variant, tags in by_tag(report).items():
        for tag, s in tags.items():
            if s.cases >= MIN_TAG_CASES:
                lines.append(f"| {variant} | {tag} | {s.cases} | {_pct(s.accuracy)} |")
    return lines


def _evidence_and_abstention(report: Report) -> list[str]:
    lines = [
        "",
        "## Evidence and abstention",
        "",
        "Validity is over the raw evidence items of the model's answer (D-109); the other two "
        "are the recall of the expected evidence lines.",
        "",
        "| Variant | Evidence validity | Answers without valid evidence | Expected evidence in "
        "excerpt | Expected evidence cited | Unknown | Low confidence | Abstention | LLM share "
        "| Cache hits | Similar hits | Fallbacks |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in report.runs:
        m = run.metrics
        lines.append(
            f"| {run.variant} | {_pct(m.evidence_validity)} "
            f"| {m.answers_without_valid_evidence} of {m.answers} "
            f"| {_pct(m.evidence_in_excerpt)} | {_pct(m.evidence_in_diagnosis)} "
            f"| {_pct(m.abstention_unknown)} | {_pct(m.abstention_low_confidence)} "
            f"| {_pct(m.abstention)} | {_pct(m.llm_share)} "
            f"| {_hits(m.cache_hits, m.cache_hit_rate)} "
            f"| {_hits(m.similar_hits, m.similar_hit_rate)} | {sum(m.fallbacks.values())} |"
        )
    return lines


def _cost_and_latency(report: Report) -> list[str]:
    """Latency of the model call, NFR-03 cost and the calibration headline (D-109)."""
    lines = [
        "",
        "## Cost and latency",
        "",
        "Model latency is the recorded one of the answer (the run that filled the store); the "
        "pipeline latency is this run, with stored answers. Cost is the stored tokens times the "
        "prices, over the diagnoses published from the model.",
        "",
        "| Variant | Model call p50 / p95 (ms) | Pipeline p50 / p95 (ms, with stored answers) "
        "| LLM diagnoses | Cost per LLM diagnosis (USD) | Cost p95 | Total cost (USD) "
        "| High-confidence accuracy | ECE |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in report.runs:
        m = run.metrics
        p95 = "n/a" if m.cost_usd_p95 is None else f"{m.cost_usd_p95:.5f}"
        ece = (
            "n/a" if m.expected_calibration_error is None else f"{m.expected_calibration_error:.3f}"
        )
        lines.append(
            f"| {run.variant} | {_model_latency(m)} "
            f"| {m.latency_ms_p50:.0f} / {m.latency_ms_p95:.0f} | {m.llm_diagnoses} "
            f"| {_cost_per_diagnosis(m)} | {p95} | {m.cost_usd_total:.4f} "
            f"| {_pct(m.high_confidence_accuracy)} | {ece} |"
        )
    return lines


def _intervals(report: Report) -> list[str]:
    lines = [
        "",
        "## Bootstrap intervals",
        "",
        "95% bootstrap intervals (1,000 resamples, fixed seed; informational, D-105):",
        "",
        "| Variant | Group | Cases | Accuracy | Macro-F1 |",
        "|---|---|---|---|---|",
    ]
    for run in report.runs:
        for group, data in run.metrics.intervals.items():
            acc, f1 = _bounds(data["accuracy"]), _bounds(data["macro_f1"])
            lines.append(f"| {run.variant} | {group} | {data['cases']} | {acc} | {f1} |")
    return lines


def _cost_per_diagnosis(m: Metrics) -> str:
    if m.cost_usd_per_llm_diagnosis is None:
        return "n/a"
    cost = f"{m.cost_usd_per_llm_diagnosis:.5f}"
    return cost + (" (no prices: cost is 0)" if m.priced is False else "")


def _model_latency(m: Metrics) -> str:
    if m.llm_latency_ms_p50 is None:
        return "n/a"
    return f"{m.llm_latency_ms_p50:.0f} / {m.llm_latency_ms_p95 or 0:.0f}"


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


def _bounds(interval: object) -> str:
    assert isinstance(interval, dict)
    return f"{interval['low'] * 100:.1f}% to {interval['high'] * 100:.1f}%"


def _flaky(report: Report) -> list[str]:
    f = report.flaky
    lines = ["", "## Flaky and fix resolution over simulated sequences (D-107)", ""]
    if f is None:
        return [*lines, "Not measured in this run."]
    return [
        *lines,
        f.note,
        "",
        "| Sequences | Correct | resolved_flaky precision / recall "
        "| resolved_by_commit precision / recall | Lab-shaped |",
        "|---|---|---|---|---|",
        f"| {f.sequences} | {f.correct} | {_pct(f.flaky_precision)} / {_pct(f.flaky_recall)} "
        f"| {_pct(f.by_commit_precision)} / {_pct(f.by_commit_recall)} "
        f"| {f.lab_correct} of {f.lab_sequences} |",
        *(["", *(f"- miss: {miss}" for miss in f.misses)] if f.misses else []),
    ]


def _hits(count: int | None, rate: float | None) -> str:
    return "n/a" if count is None else f"{count} ({_pct(rate)})"


def _per_category(metrics: Metrics) -> list[str]:
    rows = [
        "Per category:",
        "",
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


def _calibration(
    calibration: Mapping[str, Mapping[str, float]],
    deciles: Sequence[Mapping[str, float]] = (),
) -> list[str]:
    rows = ["Calibration:", "", "| Confidence | Cases | Accuracy |", "|---|---|---|"]
    for name, values in calibration.items():
        rows.append(f"| {name} | {int(values['cases'])} | {_pct(values['accuracy'])} |")
    if deciles:
        rows += ["", "By decile (only deciles with 5 or more cases):", ""]
        rows += ["| Confidence | Cases | Accuracy | Mean confidence |", "|---|---|---|---|"]
        for d in deciles:
            rows.append(
                f"| {d['from']:.1f} to {d['to']:.1f} | {int(d['cases'])} | {_pct(d['accuracy'])} "
                f"| {d['mean_confidence']:.2f} |"
            )
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
