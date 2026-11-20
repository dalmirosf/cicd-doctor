"""``python -m evals prune``: delete the answers no current configuration would use (D-100).

The configuration is (case x variant x model). Instead of re-deriving the keys, the LLM variants
run offline over the cases and every entry the cache looked up (a hit, or a repair call that a
stored answer led to) is kept. Everything else under ``evals/answers/`` is an orphan: an answer of
another prompt, of another model, of a changed case, a variance run (``*.<suffix>.json``) or a
half-written ``.tmp``. A dry run lists them and deletes nothing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cicd_doctor.core.ports import Embedder, TokenCounter
from evals.runner.run import EvalError, LLMSetup, run_evals_async


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
) -> set[Path]:
    """Run every variant offline and return the entries the cache looked up."""
    if not setups or any(s.cache is None for s in setups):
        raise EvalError("prune needs at least one model (--model or OPENAI_MODEL)")
    await run_evals_async("all", sources, cases_dir, counter, llms=setups, embedder=embedder)
    used: set[Path] = set()
    for setup in setups:
        assert setup.cache is not None
        used |= setup.cache.used
    return used


def prune(directory: Path, used: set[Path], *, apply: bool = False) -> PruneResult:
    """Keep ``used``; the rest of ``directory`` is an orphan (deleted only with ``apply``)."""
    present = (
        sorted(p for p in directory.rglob("*") if p.is_file() and p.name != ".gitkeep")
        if directory.is_dir()
        else []
    )
    kept = [p for p in present if p in used]
    orphans = [p for p in present if p not in used]
    if apply:
        for path in orphans:
            path.unlink()
        for folder in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    return PruneResult(kept=kept, orphans=orphans, deleted=apply)
