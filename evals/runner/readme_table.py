"""The comparison table of the README, generated from ``evals/baseline.json`` (SPEC §11.3, P4 (f)).

``python -m evals readme-table`` rewrites what sits between the two markers of ``README.md``;
a test fails when the README differs from what the baseline generates (the same idea as the
drift test of the injection cases). Without a baseline the block is a placeholder that says so.
"""

from pathlib import Path

from evals.runner.baseline import Baseline, BaselineVariant, load_baseline

START = "<!-- evals:table:start -->"
END = "<!-- evals:table:end -->"
DEFAULT_README = Path(__file__).resolve().parents[2] / "README.md"
NFR_03_USD = 0.01  # NFR-03: average cost per LLM diagnosis

PLACEHOLDER = (
    "_No baseline has been accepted yet (`evals/baseline.json` does not exist), so there are no "
    "numbers to show. After the first accepted baseline, `python -m evals readme-table` fills "
    "this table from it._"
)


class ReadmeError(ValueError):
    """The README has no (or a broken pair of) table markers."""


def render_table(baseline: Baseline | None) -> str:
    """The text between the markers (without them), ending in a newline."""
    if baseline is None:
        return PLACEHOLDER + "\n"
    lines = [
        "| Variant | Model | N | Accuracy | Macro-F1 | Cause match (mean 0-2) "
        "| Cost per LLM diagnosis (USD) | Model call p50 / p95 (ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for v in baseline.variants.values():
        lines.append(
            f"| {v.name} | {v.model or '-'} | {v.cases} | {v.accuracy * 100:.1f}% "
            f"| {v.macro_f1:.3f} | {_opt(v.cause_match_mean, '{:.2f}')} "
            f"| {_opt(v.cost_per_llm_diagnosis, '{:.5f}')} | {_latency(v)} |"
        )
    return "\n".join(["", *lines, "", nfr_03_line(baseline), "", _FOOTNOTE, ""])


_FOOTNOTE = (
    "Numbers come from the accepted `evals/baseline.json` (answers stored in `evals/answers/`); "
    "latency is the recorded one of the model call, not of the offline run. "
    "`n/a`: not measured for that variant."
)


def nfr_03_line(baseline: Baseline) -> str:
    """NFR-03 (average <= USD 0.01 per LLM diagnosis) over the variants that have a cost."""
    priced = [v for v in baseline.variants.values() if v.cost_per_llm_diagnosis is not None]
    if not priced:
        return "NFR-03 (average cost <= USD 0.01 per LLM diagnosis): not measured, no LLM variant."
    over = [v for v in priced if (v.cost_per_llm_diagnosis or 0) > NFR_03_USD]
    unpriced = [v.name for v in priced if v.prices is None]
    head = "NFR-03 (average cost <= USD 0.01 per LLM diagnosis): "
    if over:
        names = ", ".join(f"{v.name} ({v.cost_per_llm_diagnosis:.5f})" for v in over)
        line = f"**not met** by {names}."
    else:
        line = "met by " + ", ".join(v.name for v in priced) + "."
    if unpriced:
        line += " No prices recorded for " + ", ".join(unpriced) + ": its cost is not trusted."
    return head + line


def _opt(value: float | None, fmt: str) -> str:
    return "n/a" if value is None else fmt.format(value)


def _latency(v: BaselineVariant) -> str:
    if v.latency_ms_p50 is None:
        return "n/a"
    return f"{v.latency_ms_p50:.0f} / {(v.latency_ms_p95 or 0):.0f}"


def replace_block(readme: str, baseline: Baseline | None) -> str:
    """``readme`` with the block between the markers regenerated (idempotent)."""
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ReadmeError(f"README.md needs exactly one {START} and one {END} marker")
    head, rest = readme.split(START)
    _old, tail = rest.split(END)
    return f"{head}{START}\n{render_table(baseline).lstrip(chr(10))}{END}{tail}"


def check_readme(readme_path: Path, baseline_path: Path) -> bool:
    """True when the README already matches the baseline."""
    text = _read(readme_path)
    return replace_block(text, load_baseline(baseline_path)) == text


def write_readme(readme_path: Path, baseline_path: Path) -> bool:
    """Rewrite the block; True when the file changed."""
    text = _read(readme_path)
    new = replace_block(text, load_baseline(baseline_path))
    if new == text:
        return False
    readme_path.write_text(new, encoding="utf-8", newline="\n")
    return True


def _read(path: Path) -> str:
    return path.read_text("utf-8").replace("\r\n", "\n")
