"""Command line: ``python -m evals run`` and ``python -m evals capture`` (SPEC §11.3, D-61)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from evals.runner.capture import (
    DEFAULT_MANIFEST,
    CaptureError,
    capture,
    capture_all,
    load_manifest,
)
from evals.runner.cases import DEFAULT_CASES_DIR, SOURCES
from evals.runner.injection import write_cases
from evals.runner.live import DEFAULT_CACHE_DIR, LiveSettings, build_setups, parse_prices
from evals.runner.report import DEFAULT_REPORTS_DIR, write_report
from evals.runner.run import EvalError, run_evals
from evals.runner.variants import ALL_VARIANTS, LLM_VARIANTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run eval variants and write a report")
    run.add_argument("--variant", default="rules", choices=[*ALL_VARIANTS, "all"])
    run.add_argument("--sources", default="lab", help=f"comma-separated: {', '.join(SOURCES)}")
    run.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    run.add_argument("--out-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    run.add_argument(
        "--model",
        help="comma-separated model ids for the llm variants (default: OPENAI_MODEL from .env)",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="call OpenAI (OPENAI_API_KEY from .env) and fill the cache; without it only the "
        "cache is read",
    )
    run.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="MODEL=IN,OUT",
        help="USD per million tokens for a model (default: LLM_PRICE_* from .env)",
    )
    run.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)

    commands.add_parser(
        "write-injection",
        help="regenerate evals/cases/injection from evals/runner/injection.py",
    )

    cap = commands.add_parser(
        "capture",
        help="turn lab runs into eval cases (uses gh)",
        description="Either --all (the latest failed runs of every case, skipping runs already "
        "captured) or one run with --run-id and --case.",
    )
    cap.add_argument("--repo", required=True, help="owner/name of the lab repository")
    cap.add_argument("--all", action="store_true", help="capture every case that needs runs")
    cap.add_argument("--run-id", type=int)
    cap.add_argument("--attempt", type=int, default=1)
    cap.add_argument("--case", action="append", help="case id in lab/cases.yaml (repeatable)")
    cap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    cap.add_argument("--out-dir", type=Path, default=DEFAULT_CASES_DIR / "lab")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "write-injection":
            return _write_injection()
        return _capture(args)
    except (EvalError, CaptureError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _write_injection() -> int:
    roots = write_cases(DEFAULT_CASES_DIR / "injection")
    print(f"wrote {len(roots)} injection cases to {DEFAULT_CASES_DIR / 'injection'}")
    return 0


def _run(args: argparse.Namespace) -> int:
    sources = [source.strip() for source in args.sources.split(",") if source.strip()]
    wants_llm = args.variant in (*LLM_VARIANTS, "all")
    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    llms = []
    if wants_llm:
        settings = LiveSettings()
        models = models or ([settings.openai_model] if settings.openai_model else [])
        if models:
            llms = build_setups(
                models,
                live=args.live,
                cache_dir=args.cache_dir,
                prices=parse_prices(args.price),
                settings=settings,
            )
    report = run_evals(args.variant, sources, args.cases_dir, TiktokenCounter(), llms=llms)
    json_path, md_path = write_report(report, args.out_dir)
    for run in report.runs:
        m = run.metrics
        print(
            f"{run.variant}: {m.cases} cases, accuracy {m.accuracy:.1%}, macro-F1 {m.macro_f1:.3f}"
            + (
                f", cost ${m.cost_usd_total:.4f}, p95 {m.latency_ms_p95:.0f} ms"
                if m.llm_calls
                else ""
            )
            + (f", attacks won {m.attacks_won}/{m.attack_cases}" if m.attack_cases else "")
        )
    for variant, reason in report.skipped.items():
        print(f"{variant}: not run ({reason})")
    print(f"report: {json_path} and {md_path}")
    return 0


def _capture(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    if args.all:
        captured, notes = capture_all(
            args.repo, manifest=manifest, only=args.case or (), out_dir=args.out_dir
        )
    elif args.run_id is not None and args.case and len(args.case) == 1:
        one = capture(
            args.repo,
            args.run_id,
            args.case[0],
            attempt=args.attempt,
            manifest=manifest,
            out_dir=args.out_dir,
        )
        captured, notes = [one], []
    else:
        raise CaptureError("use --all, or --run-id with exactly one --case")
    for item in captured:
        print(
            f"wrote {item.path} (job {item.job_id}, {item.lines} lines, "
            f"{item.redactions} redactions, evidence lines {item.evidence_lines})"
        )
        for pattern in item.missing_evidence:
            print(
                f"warning: {item.path.name}: evidence {pattern!r} matched no line", file=sys.stderr
            )
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    return 0
