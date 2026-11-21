"""``python -m evals baseline`` and ``python -m evals check`` (D-105, D-106).

``check`` runs offline from the committed answers (no key, no network, no money) and takes the
models, the prices and the variants from the baseline it compares with. ``baseline --accept`` is run
by a person only: the CI never writes the baseline.
"""

import argparse
import dataclasses
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import prompt_fingerprint
from evals.runner.baseline import DEFAULT_BASELINE, Baseline, BaselineError, load_baseline
from evals.runner.cases import DEFAULT_CASES_DIR
from evals.runner.gate import (
    CHECK_SOURCES,
    EXIT_RUNNER,
    EXIT_STALE,
    GateError,
    accept_problems,
    base_name,
    build_baseline,
    cases_hash,
    dump_baseline,
    evaluate,
    model_of,
    render_summary,
)
from evals.runner.judge_cli import add_run_arguments, apply_judge
from evals.runner.live import (
    DEFAULT_CACHE_DIR,
    LiveSettings,
    build_embedder,
    build_setups,
    parse_prices,
)
from evals.runner.report import DEFAULT_REPORTS_DIR, Report, write_report
from evals.runner.run import EvalError, LLMSetup, run_evals
from evals.runner.variants import LLM_VARIANTS, SIMILAR_VARIANT

PIPELINE_LOGGER = "cicd_doctor.core.diagnosis.pipeline"


def add_gate_commands(commands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    check = commands.add_parser(
        "check",
        help="compare an offline run with evals/baseline.json (the CI gate, D-105, D-106)",
        description="Exit codes: 0 ok, 1 regression, 2 injection success, 3 incomplete answers, "
        "4 stale or missing baseline, 5 runner error. The most serious wins: 2, 3, 4, 1.",
    )
    check.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    check.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    check.add_argument("--answers-dir", type=Path, default=DEFAULT_CACHE_DIR)
    check.add_argument("--out-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    check.add_argument(
        "--summary", type=Path, help="write the Markdown summary here (for $GITHUB_STEP_SUMMARY)"
    )

    accept = commands.add_parser(
        "baseline",
        help="write evals/baseline.json from a run of the stored answers (a person, never the CI)",
        description="Without --accept it only shows what would be written.",
    )
    accept.add_argument("--accept", action="store_true", help="write the file")
    accept.add_argument("--out", type=Path, default=DEFAULT_BASELINE)
    accept.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    accept.add_argument("--answers-dir", type=Path, default=DEFAULT_CACHE_DIR)
    accept.add_argument("--model", help="comma-separated model ids (default: OPENAI_MODEL)")
    accept.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT")
    accept.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    add_run_arguments(accept)


def check(args: argparse.Namespace) -> int:
    """Exit code of the gate. Prints ids and numbers only."""
    try:
        try:
            baseline = load_baseline(args.baseline)
        except BaselineError as exc:
            raise GateError(f"{args.baseline.name}: {exc}") from None
        if baseline is None:
            text = (
                "# evals check: stale baseline (exit 4)\n\n"
                f"There is no `{args.baseline.name}`: nothing was accepted yet. A person runs "
                "`python -m evals baseline --accept` and commits it (D-105).\n"
            )
            return _finish(text, args, EXIT_STALE)
        report = _run_offline(baseline, args)
        outcome = evaluate(
            report, baseline, current_cases_hash=cases_hash(args.cases_dir, CHECK_SOURCES)
        )
    except (GateError, EvalError, ValueError, OSError) as exc:
        text = f"# evals check: runner error (exit 5)\n\n{type(exc).__name__}: {exc}\n"
        return _finish(text, args, EXIT_RUNNER)
    json_path, md_path = write_report(report, args.out_dir)
    text = render_summary(outcome)
    print(f"report: {json_path} and {md_path}")
    return _finish(text, args, outcome.code)


def _finish(text: str, args: argparse.Namespace, code: int) -> int:
    print(text)
    if args.summary is not None:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8", newline="\n")
    return code


def _run_offline(baseline: Baseline, args: argparse.Namespace) -> Report:
    """The variants of the baseline over lab + public + injection, from the stored answers."""
    wanted: dict[str, list[str]] = {}  # base variant -> models
    prices: dict[str, CostModel] = {}
    for name, variant in baseline.variants.items():
        base = base_name(name)
        wanted.setdefault(base, [])
        model = model_of(name)
        if model is not None and model not in wanted[base]:
            wanted[base].append(model)
            p = variant.prices
            prices[model] = CostModel(
                input_per_mtok=_decimal(p.input_per_mtok if p else 0),
                output_per_mtok=_decimal(p.output_per_mtok if p else 0),
            )
    return _run(wanted, prices, args, sources=CHECK_SOURCES, baseline=baseline)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _run(
    wanted: dict[str, list[str]],
    prices: dict[str, CostModel],
    args: argparse.Namespace,
    *,
    sources: tuple[str, ...],
    baseline: Baseline | None,
) -> Report:
    models = sorted({m for ms in wanted.values() for m in ms})
    settings = LiveSettings()
    setups: dict[str, LLMSetup] = {}
    if models:
        for setup in build_setups(
            models, live=False, cache_dir=args.answers_dir, prices=prices, settings=settings
        ):
            setups[setup.label] = setup
    embedder = build_embedder(settings) if SIMILAR_VARIANT in wanted else None
    counter = TiktokenCounter()
    runs = []
    first: Report | None = None
    logger = logging.getLogger(PIPELINE_LOGGER)
    level = logger.level
    logger.setLevel(logging.ERROR)  # a missing answer is reported once, grouped (exit 3)
    try:
        for base, base_models in wanted.items():
            report = run_evals(
                base,
                sources,
                args.cases_dir,
                counter,
                llms=[setups[m] for m in base_models],
                embedder=embedder if base == SIMILAR_VARIANT else None,
            )
            first = first or report
            runs += report.runs
    finally:
        logger.setLevel(level)
    assert first is not None
    return dataclasses.replace(first, runs=runs, baseline=baseline)


def accept(args: argparse.Namespace) -> int:
    settings = LiveSettings()
    models = [m.strip() for m in (args.model or "").split(",") if m.strip()]
    models = models or ([settings.openai_model] if settings.openai_model else [])
    prices = parse_prices(args.price)
    for model in models:
        prices.setdefault(
            model,
            CostModel(
                input_per_mtok=settings.llm_price_input_per_mtok or _decimal(0),
                output_per_mtok=settings.llm_price_output_per_mtok or _decimal(0),
            ),
        )
    wanted: dict[str, list[str]] = {"rules": []}
    if models:
        wanted.update({variant: list(models) for variant in LLM_VARIANTS})
    sources = list(CHECK_SOURCES)
    report = _run(wanted, prices, args, sources=CHECK_SOURCES, baseline=None)
    report = apply_judge(args, report, sources)
    problems = accept_problems(report)
    if problems:
        print("refusing to accept this run as a baseline:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    data = build_baseline(
        report,
        prices=prices,
        current_cases_hash=cases_hash(args.cases_dir, CHECK_SOURCES),
        created_at=datetime.now(UTC),
    )
    text = dump_baseline(data)
    if not args.accept:
        print(text)
        print("dry run: pass --accept to write", args.out)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} (prompt fingerprint {prompt_fingerprint()[:12]})")
    return 0
