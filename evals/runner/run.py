"""``python -m evals run``: run variants over the cases and write a report (SPEC §11.3)."""

import asyncio
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.adapters.memory.ledger import InMemoryUsageLedger
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from cicd_doctor.core.ports import Embedder, LLMClient, TokenCounter
from evals.runner.cases import Case, load_cases
from evals.runner.flaky import run_flaky_metric
from evals.runner.history import SimilarHistory, history_order
from evals.runner.metrics import compute
from evals.runner.recording import RecordingLLM
from evals.runner.report import Report, VariantRun
from evals.runner.variants import (
    ALL_VARIANTS,
    LLM_VARIANTS,
    SIMILAR_VARIANT,
    VARIANTS,
    CaseResult,
    run_llm_case,
)

NEEDS_A_MODEL = (
    "needs a model: pass --model (and --live to call OpenAI, otherwise the cache is used)"
)


class EvalError(Exception):
    """A problem with the request (unknown variant, no cases): reported, not a crash."""


@dataclass(frozen=True)
class LLMSetup:
    """A model to evaluate: the client (live or cached), its name for the report, its prices."""

    client: LLMClient
    label: str
    cost: CostModel
    max_output_tokens: int = 800
    recorder: RecordingLLM | None = None  # what the model answered, per case (D-100)
    cache: CachedLLMClient | None = None  # the answer store behind the client (prune reads it)


def resolve_variants(requested: str, have_llm: bool = False) -> tuple[list[str], dict[str, str]]:
    """``(variants to run, skipped variant -> reason)`` for ``--variant``."""
    runnable = ["rules", *(LLM_VARIANTS if have_llm else ())]
    if requested == "all":
        return runnable, {v: NEEDS_A_MODEL for v in LLM_VARIANTS if not have_llm}
    if requested in runnable:
        return [requested], {}
    if requested in LLM_VARIANTS:
        raise EvalError(f"variant {requested!r} {NEEDS_A_MODEL}")
    raise EvalError(f"unknown variant {requested!r}; choose from {', '.join(ALL_VARIANTS)} or all")


def run_evals(
    variant: str,
    sources: Sequence[str],
    cases_dir: Path,
    counter: TokenCounter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    git_sha: Callable[[], str] | None = None,
    llms: Sequence[LLMSetup] = (),
    embedder: Embedder | None = None,
) -> Report:
    """The synchronous entry point (the CLI). In a running event loop use ``run_evals_async``."""
    return asyncio.run(
        run_evals_async(variant, sources, cases_dir, counter, now, git_sha, llms, embedder)
    )


async def run_evals_async(
    variant: str,
    sources: Sequence[str],
    cases_dir: Path,
    counter: TokenCounter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    git_sha: Callable[[], str] | None = None,
    llms: Sequence[LLMSetup] = (),
    embedder: Embedder | None = None,
) -> Report:
    variants, skipped = resolve_variants(variant, have_llm=bool(llms))
    if SIMILAR_VARIANT in variants and embedder is None:
        raise EvalError(f"variant {SIMILAR_VARIANT!r} needs an embedder (D-86)")
    cases = load_cases(cases_dir, sources)
    if not cases:
        raise EvalError(
            f"no cases for source(s) {', '.join(sources)} under {cases_dir}; "
            "capture lab cases with `python -m evals capture` (see lab/README.md)"
        )
    runs: list[VariantRun] = []
    for name in variants:
        if name in LLM_VARIANTS:
            for setup in llms:
                results = await _run_llm(name, setup, cases, counter, embedder)
                runs.append(
                    _variant_run(
                        f"{name} [{setup.label}]",
                        results,
                        name == SIMILAR_VARIANT,
                        priced=setup.cost.input_per_mtok > 0 or setup.cost.output_per_mtok > 0,
                    )
                )
        else:
            results = [VARIANTS[name](case, counter) for case in cases]
            runs.append(_variant_run(name, results))
    return Report(
        generated_at=now(),
        git_sha=(git_sha or current_git_sha)(),
        sources=list(sources),
        runs=runs,
        skipped=skipped,
        flaky=run_flaky_metric(),
    )


def _variant_run(
    name: str,
    results: list[CaseResult],
    with_history: bool = False,
    priced: bool | None = None,
) -> VariantRun:
    return VariantRun(
        variant=name,
        metrics=compute(results, with_history=with_history, priced=priced),
        results=results,
    )


async def _run_llm(
    variant: str,
    setup: LLMSetup,
    cases: Sequence[Case],
    counter: TokenCounter,
    embedder: Embedder | None = None,
) -> list[CaseResult]:
    """Every case through the production pipeline, one after the other (kind to rate limits).

    ``llm+hints+similar`` runs them in history order (per repository, by ``captured_at``) over a
    fresh in-memory history, and the results come back in the order of ``cases`` (D-86).
    """
    pipeline = DiagnoseJob(
        llm=setup.client,
        ledger=InMemoryUsageLedger(
            SystemClock(), calls_per_installation=10**9, daily_budget_usd=Decimal(10**9)
        ),
        cost=setup.cost,
        max_output_tokens=setup.max_output_tokens,
    )
    history = SimilarHistory(embedder) if variant == SIMILAR_VARIANT and embedder else None
    done: dict[str, CaseResult] = {}
    for case in history_order(cases) if history else cases:
        if setup.recorder is not None:
            setup.recorder.begin(case.id)
        done[case.id] = await run_llm_case(
            case,
            counter,
            variant=f"{variant} [{setup.label}]",
            pipeline=pipeline,
            use_hints=variant != "llm",
            history=history,
            recorder=setup.recorder,
        )
    return [done[case.id] for case in cases]


def current_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    return result.stdout.strip() or "nogit"
