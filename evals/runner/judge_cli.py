"""The command line of the cause judge: `run --judge` and `calibrate` (D-103, D-104).

Kept apart from ``cli.py`` so the main parser only registers it.
"""

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from evals.runner.calibration import (
    DEFAULT_CALIBRATION_DIR,
    DEFAULT_SEED,
    KEY_FILE,
    LABELS_FILE,
    SCORES_FILE,
    load_key,
    load_labels,
    score_pairs,
    scores_document,
    select_pairs,
    write_report,
    write_sheet,
)
from evals.runner.cases import DEFAULT_CASES_DIR, load_cases
from evals.runner.judge import (
    DEFAULT_JUDGE_DIR,
    JUDGE_PROMPT_VERSION,
    CauseMatch,
    build_judge,
    judge_fingerprint,
    judge_results,
)
from evals.runner.live import DEFAULT_CACHE_DIR, LiveSettings, build_setups, parse_prices
from evals.runner.report import Report
from evals.runner.run import EvalError, run_evals
from evals.runner.variants import CaseResult


def add_judge_arguments(parser: argparse.ArgumentParser) -> None:
    """The judge flags, shared by ``run`` and ``calibrate score``."""
    parser.add_argument("--judge-model", help="the judge's own model (default: JUDGE_MODEL)")
    parser.add_argument(
        "--judge-price",
        metavar="MODEL=IN,OUT",
        help="USD per million tokens for the judge (default: JUDGE_PRICE_* from .env)",
    )
    parser.add_argument("--judge-cache-dir", type=Path, default=DEFAULT_JUDGE_DIR)


def add_run_arguments(run: argparse.ArgumentParser) -> None:
    run.add_argument(
        "--judge",
        action="store_true",
        help="score the cause of every answer with the judge (off by default: the report then "
        "says cause match is not judged); --live also fills the judge's own cache",
    )
    add_judge_arguments(run)


def add_calibrate_command(commands: Any) -> None:
    cal = commands.add_parser(
        "calibrate",
        help="calibrate the cause judge against your labels (D-104)",
        description="sheet: write the blind sheet to label. score: run the judge on the pairs you "
        "labelled. report: the agreement, kappa and the verdict.",
    )
    cal.add_argument("step", choices=["sheet", "score", "report"])
    cal.add_argument("--dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    cal.add_argument("--seed", type=int, default=DEFAULT_SEED)
    cal.add_argument("--sources", default="lab,public")
    cal.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    cal.add_argument("--model", help="model id whose cached answers feed the sheet")
    cal.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    cal.add_argument("--live", action="store_true", help="score: call OpenAI for the judge")
    add_judge_arguments(cal)


def _judge(args: argparse.Namespace, *, live: bool) -> Any:
    price = None
    if args.judge_price:
        prices = parse_prices([args.judge_price])
        price = next(iter(prices.values()))
    try:
        return build_judge(args.judge_model, live=live, price=price, cache_dir=args.judge_cache_dir)
    except ValueError as exc:
        raise EvalError(str(exc)) from None


def apply_judge(args: argparse.Namespace, report: Report, sources: list[str]) -> Report:
    """The report with its cause-match section, or untouched without ``--judge``."""
    if not args.judge:
        return report
    judge = _judge(args, live=args.live)
    cases = load_cases(args.cases_dir, sources)
    results = [result for run in report.runs for result in run.results]
    judgments = asyncio.run(judge_results(results, cases, judge))
    return dataclasses.replace(
        report,
        cause_match=CauseMatch(
            judge_model=judge.model,
            prompt_version=JUDGE_PROMPT_VERSION,
            fingerprint=judge_fingerprint(),
            judgments=judgments,
        ),
    )


def calibrate(args: argparse.Namespace) -> int:
    if args.step == "sheet":
        return _sheet(args)
    if args.step == "score":
        return _score(args)
    return _report(args)


def _sheet_results(args: argparse.Namespace, sources: list[str]) -> list[CaseResult]:
    counter = TiktokenCounter()
    results = [
        r for run in run_evals("rules", sources, args.cases_dir, counter).runs for r in run.results
    ]
    settings = LiveSettings()
    model = args.model or settings.openai_model
    if model:
        llms = build_setups([model], live=False, cache_dir=args.cache_dir, settings=settings)
        for variant in ("llm", "llm+hints"):
            report = run_evals(variant, sources, args.cases_dir, counter, llms=llms)
            results += [r for run in report.runs for r in run.results]
    return results


def _sheet(args: argparse.Namespace) -> int:
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    cases = load_cases(args.cases_dir, sources)
    pairs = select_pairs(_sheet_results(args, sources), cases, args.seed)
    sheet = write_sheet(pairs, args.seed, args.dir)
    partial = sum(1 for p in pairs if p.likely_partial)
    print(f"wrote {len(pairs)} pairs ({partial} likely partial) to {sheet}")
    print(f"label them in {args.dir / LABELS_FILE} (0, 1 or 2), then run `calibrate score`")
    return 0


def _score(args: argparse.Namespace) -> int:
    pairs = load_key(args.dir / KEY_FILE)
    labels = load_labels(args.dir / LABELS_FILE, [p.pair_id for p in pairs])
    if not labels:
        raise EvalError(f"{LABELS_FILE} has no labels yet")
    judge = _judge(args, live=args.live)
    scored = asyncio.run(score_pairs(pairs, labels, judge))
    document = scores_document(scored, judge_model=judge.model, prompt_version=JUDGE_PROMPT_VERSION)
    path = args.dir / SCORES_FILE
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"scored {len(scored)} labelled pairs with {judge.model}: {path}")
    return 0


def _report(args: argparse.Namespace) -> int:
    document = json.loads((args.dir / SCORES_FILE).read_text("utf-8"))
    json_path, md_path = write_report(document, args.dir)
    print(f"report: {json_path} and {md_path}")
    return 0
