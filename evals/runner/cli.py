"""Command line: ``python -m evals run`` and ``python -m evals capture`` (SPEC §11.3, D-61)."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from evals.runner.baseline import DEFAULT_BASELINE, load_baseline
from evals.runner.capture import (
    DEFAULT_MANIFEST,
    CaptureError,
    capture,
    capture_all,
    load_manifest,
)
from evals.runner.cases import DEFAULT_CASES_DIR, SOURCES
from evals.runner.injection import write_cases
from evals.runner.judge_cli import add_calibrate_command, add_run_arguments, apply_judge, calibrate
from evals.runner.live import (
    DEFAULT_CACHE_DIR,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_COST_USD,
    LiveSettings,
    build_embedder,
    build_setups,
    parse_prices,
)
from evals.runner.prune import prune, used_entries
from evals.runner.public_cli import PUBLIC_COMMANDS, add_public_commands, run_public
from evals.runner.readme_table import DEFAULT_README, check_readme, write_readme
from evals.runner.recording import BudgetExceeded, SpendGuard
from evals.runner.report import DEFAULT_REPORTS_DIR, write_report
from evals.runner.run import EvalError, run_evals
from evals.runner.variants import ALL_VARIANTS, LLM_VARIANTS, SIMILAR_VARIANT

EXIT_BUDGET = 5  # a live run stopped at --max-cost-usd or --max-calls (D-108)


def _positive_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run eval variants and write a report")
    run.add_argument("--variant", default="rules", choices=[*ALL_VARIANTS, "all"])
    run.add_argument("--sources", default="lab", help=f"comma-separated: {', '.join(SOURCES)}")
    run.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    run.add_argument("--out-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    run.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="accepted baseline for the 'Verdict changes' section (default: evals/baseline.json; "
        "the report says so when it does not exist)",
    )
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
    run.add_argument(
        "--cache-dir",
        "--answers-dir",
        dest="cache_dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="where the LLM answers live (default: evals/answers, committed, D-100)",
    )
    run.add_argument(
        "--max-cost-usd",
        type=_positive_decimal,
        default=DEFAULT_MAX_COST_USD,
        help=f"with --live: stop after this much spend (default {DEFAULT_MAX_COST_USD}, D-108)",
    )
    run.add_argument(
        "--max-calls",
        type=_positive_int,
        default=DEFAULT_MAX_CALLS,
        help=f"with --live: stop after this many model calls (default {DEFAULT_MAX_CALLS})",
    )
    run.add_argument(
        "--no-cache-read",
        action="store_true",
        help="with --live: always call the model and write each answer next to the official "
        "one with a run suffix, never over it (to measure variance)",
    )

    prune_cmd = commands.add_parser(
        "prune",
        help="delete the answers no current (case x variant x model) would use",
        description="Dry run unless --apply. Runs the LLM variants offline and keeps every "
        "entry they look up.",
    )
    prune_cmd.add_argument("--sources", default=",".join(SOURCES))
    prune_cmd.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    prune_cmd.add_argument("--model", help="comma-separated model ids (default: OPENAI_MODEL)")
    prune_cmd.add_argument("--answers-dir", type=Path, default=DEFAULT_CACHE_DIR)
    prune_cmd.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    add_run_arguments(run)
    add_calibrate_command(commands)

    table = commands.add_parser(
        "readme-table",
        help="regenerate the comparison table of README.md from evals/baseline.json",
    )
    table.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    table.add_argument("--readme", type=Path, default=DEFAULT_README)
    table.add_argument("--check", action="store_true", help="only check; exit 1 when it differs")

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

    add_public_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "prune":
            return _prune(args)
        if args.command == "calibrate":
            return calibrate(args)
        if args.command == "readme-table":
            return _readme_table(args)
        if args.command == "write-injection":
            return _write_injection()
        if args.command in PUBLIC_COMMANDS:
            return run_public(args)
        return _capture(args)
    except BudgetExceeded as exc:
        print(
            f"stopped: {exc.reason}. The answers already written stay in the answers folder; "
            "raise the cap to continue.",
            file=sys.stderr,
        )
        return EXIT_BUDGET
    except (EvalError, CaptureError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _readme_table(args: argparse.Namespace) -> int:
    if args.check:
        if check_readme(args.readme, args.baseline):
            print("README table is up to date")
            return 0
        print("README table differs from the baseline: run `python -m evals readme-table`")
        return 1
    changed = write_readme(args.readme, args.baseline)
    print(f"{'updated' if changed else 'unchanged'}: {args.readme}")
    return 0


def _write_injection() -> int:
    roots = write_cases(DEFAULT_CASES_DIR / "injection")
    print(f"wrote {len(roots)} injection cases to {DEFAULT_CASES_DIR / 'injection'}")
    return 0


def _run(args: argparse.Namespace) -> int:
    sources = [source.strip() for source in args.sources.split(",") if source.strip()]
    wants_llm = args.variant in (*LLM_VARIANTS, "all")
    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    llms = []
    embedder = None
    guard = None
    if args.live:
        guard = SpendGuard(max_cost_usd=args.max_cost_usd, max_calls=args.max_calls)
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
                guard=guard,
                read_cache=not args.no_cache_read,
                run_suffix=datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ"),
            )
            if args.variant in (SIMILAR_VARIANT, "all"):
                embedder = build_embedder(settings)
    report = run_evals(
        args.variant,
        sources,
        args.cases_dir,
        TiktokenCounter(),
        llms=llms,
        embedder=embedder,
        baseline=load_baseline(args.baseline),
    )
    report = apply_judge(args, report, sources)
    json_path, md_path = write_report(report, args.out_dir)
    if guard is not None:
        spent = f"${guard.cost_usd:.4f} of ${guard.max_cost_usd}"
        print(f"spent {spent}, {guard.calls} of {guard.max_calls} calls")
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


def _prune(args: argparse.Namespace) -> int:
    settings = LiveSettings()
    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    models = models or ([settings.openai_model] if settings.openai_model else [])
    if not models:
        raise EvalError("prune needs a model (--model or OPENAI_MODEL)")
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    setups = build_setups(models, live=False, cache_dir=args.answers_dir, settings=settings)
    used = asyncio.run(
        used_entries(setups, sources, args.cases_dir, TiktokenCounter(), build_embedder(settings))
    )
    result = prune(args.answers_dir, used, apply=args.apply)
    verb = "deleted" if result.deleted else "would delete"
    for path in result.orphans:
        print(f"{verb}: {path.relative_to(args.answers_dir)}")
    print(
        f"{len(result.kept)} kept, {len(result.orphans)} {verb.replace('would delete', 'orphan')}"
    )
    if result.orphans and not result.deleted:
        print("dry run: pass --apply to delete them")
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
