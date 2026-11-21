"""``python -m evals prune``: delete the answers no current configuration would use (D-100).

The configuration is (case x variant x model). Instead of re-deriving the keys, the LLM variants
run offline over the cases and every entry the cache looked up (a hit, or a repair call that a
stored answer led to) is kept. Everything else under ``evals/answers/`` is an orphan: an answer of
another prompt, of another model, of a changed case, a variance run (``*.<suffix>.json``) or a
half-written ``.tmp``. A dry run lists them and deletes nothing.

The judge's own cache (``evals/answers/judge/``, D-103) is keyed by something else: the judge model
and the four texts of an answer, so only a judge run can say which entries are current. It is
therefore **never touched by default**. With ``--judge-model`` the judge runs offline over the same
results and over the calibration pairs, and its lookups are the entries to keep: a judgement of an
answer no current case produces (or of another judge model or prompt) is an orphan then (D-115).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cicd_doctor.core.ports import Embedder, TokenCounter
from evals.runner.calibration import Pair, score_pairs
from evals.runner.cases import Case
from evals.runner.judge import LLMJudge, judge_results
from evals.runner.run import EvalError, LLMSetup, run_evals_async
from evals.runner.variants import CaseResult

JUDGE_FOLDER = "judge"


@dataclass(frozen=True)
class PruneResult:
    kept: list[Path]
    orphans: list[Path]
    deleted: bool


async def used_entries(
    setups: Sequence[LLMSetup],
    sources: Sequence[str],
    cases_dir: Path,
    counter: TokenCounter,
    embedder: Embedder,
    *,
    results_out: list[CaseResult] | None = None,
) -> set[Path]:
    """Run every variant offline and return the entries the cache looked up.

    ``results_out`` (optional) receives every result of the run, for the judge's lookups.
    """
    if not setups or any(s.cache is None for s in setups):
        raise EvalError("prune needs at least one model (--model or OPENAI_MODEL)")
    report = await run_evals_async(
        "all", sources, cases_dir, counter, llms=setups, embedder=embedder
    )
    if results_out is not None:
        results_out.extend(result for run in report.runs for result in run.results)
    used: set[Path] = set()
    for setup in setups:
        assert setup.cache is not None
        used |= setup.cache.used
    return used


async def judge_used_entries(
    judge: LLMJudge,
    results: Sequence[CaseResult],
    cases: Sequence[Case],
    pairs: Sequence[Pair] = (),
) -> set[Path]:
    """The judge cache entries a current run would look up: its judgements and the calibration.

    ``judge`` must be offline (no client): a miss is an error and nothing is called or written.
    """
    await judge_results(results, cases, judge)
    await score_pairs(pairs, {pair.pair_id: 0 for pair in pairs}, judge)
    return set(judge.used)


def prune(
    directory: Path,
    used: set[Path],
    *,
    apply: bool = False,
    judge_used: set[Path] | None = None,
) -> PruneResult:
    """Keep ``used``; the rest of ``directory`` is an orphan (deleted only with ``apply``).

    ``directory/judge`` is left alone unless ``judge_used`` is given (D-115): then its entries are
    kept when in ``judge_used`` and are orphans otherwise.
    """
    judge_dir = directory / JUDGE_FOLDER
    keep = set(used)
    if judge_used is not None:
        keep |= judge_used
    present = (
        sorted(
            p
            for p in directory.rglob("*")
            if p.is_file()
            and p.name != ".gitkeep"
            and (judge_used is not None or judge_dir not in p.parents)
        )
        if directory.is_dir()
        else []
    )
    kept = [p for p in present if p in keep]
    orphans = [p for p in present if p not in keep]
    if apply:
        for path in orphans:
            path.unlink()
        for folder in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    return PruneResult(kept=kept, orphans=orphans, deleted=apply)
