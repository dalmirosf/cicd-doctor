"""The report of a stored analysis, shared by the diagnosis and the comment rewrite (D-84).

Everything here is recomputed from the store: the baselines and the cache origins are part of the
stored record, and the similar failures are searched again (they are not in the checkpoint, D-82).
"""

from collections.abc import Sequence

from cicd_doctor.core.diagnosis.merge import baseline_to_show
from cicd_doctor.core.models import DiagnosisReport, RunAnalysisRecord, RunRef, SimilarFailure
from cicd_doctor.core.ports import FailureStore


async def find_similar(
    store: FailureStore,
    repo_id: int,
    embedding: Sequence[float] | None,
    *,
    k: int,
    min_score: float,
    exclude_run_id: int,
) -> list[SimilarFailure]:
    """The past failures of this repository that look like one job (D-82), best first. A job
    with no embedding or ``k = 0`` has none. The store never returns a fork, a failure of this
    run or one of another repository (D-79)."""
    if embedding is None or k <= 0:
        return []
    return await store.similar(repo_id, embedding, k, min_score, exclude_run_id=exclude_run_id)


async def build_report(
    store: FailureStore,
    run: RunRef,
    record: RunAnalysisRecord,
    *,
    similar_k: int,
    similar_min_score: float,
) -> DiagnosisReport:
    """The report of a stored analysis. ``run`` carries the pull request that was found; each
    failure carries its current status (so a resolved one renders its banner)."""
    baselines = {
        item.failure.id: baseline
        for item in record.items
        if (baseline := baseline_to_show(item.rules, item.published)) is not None
    }
    origins = {
        item.failure.id: item.cached_from_run_id
        for item in record.items
        if item.cached_from_run_id is not None
    }
    similar = {
        item.failure.id: found
        for item in record.items
        if (
            found := await find_similar(
                store,
                run.repo_id,
                item.failure.embedding,
                k=similar_k,
                min_score=similar_min_score,
                exclude_run_id=run.run_id,
            )
        )
    }
    return DiagnosisReport(
        run=run,
        items=[(item.failure, item.published) for item in record.items],
        similar=similar,
        rules_baseline=baselines,
        cached_from_runs=origins,
    )
