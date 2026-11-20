"""Use case: diagnose a failed run attempt and publish it (SPEC §7.1, D-70, D-71, D-75).

The diagnosis of an attempt is computed once and published as many times as it takes, always from
what is stored:

1. a guard: when a newer attempt already published the check run, there is nothing to do;
2. the check run goes ``in_progress`` and its id is stored right away (under the publication lock);
3. the stored analysis of the attempt, or else jobs -> logs -> excerpts and rules -> one batch of
   embeddings of the signatures (D-77) -> the LLM per job -> ``save_run_analysis``. That commit
   is the checkpoint: the LLM and the embedder are only ever called before it, so a retry after
   it downloads no log, calls no LLM, embeds nothing and reserves no budget;
4. the check run is completed from the stored analysis (under the lock, guarded again);
5. the sticky comment is upserted from the stored analysis (under the lock of its own key).

A ``cancelled`` run (D-73) goes through a gate before step 1 writes anything: the jobs of the
attempt are listed and the annotations of its cancelled jobs are read, and only when one says the
job exceeded its ``timeout-minutes`` does the flow continue (that job then counts as
``timed_out``). Otherwise the call returns at once, with no check run, comment, checkpoint or
budget reservation: a manual cancellation or a ``concurrency: cancel-in-progress`` stays silent.
The gate is recomputed on a retry, unless the analysis is already stored.

An ``explain`` call (D-94, D-95) runs the same steps over an attempt that is usually already
published: the check run it finds for this very attempt is edited in place (not restarted), the
failures whose published diagnosis is a rules one are analysed again from what is stored with the
LLM allowed (``upgrade_published``, no log is downloaded), and the check run and the comment are
rewritten from the stored result. An attempt with no checkpoint yet is analysed normally.

Steps 2, 4 and 5 are idempotent, so a failure while publishing is retried by running the use case
again. Nothing here imports an adapter: the ports and the small ``DiagnoseConfig`` are injected.
Logs, excerpts and the text of a diagnosis are never logged.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID

from cicd_doctor.core.analysis import JobAnalysis, analysis_from_stored, analyze_jobs
from cicd_doctor.core.diagnosis.cache import reuse_cached
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext, JobDiagnosis
from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.errors import LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.logs import ExcerptConfig
from cicd_doctor.core.logs.select import cancelled_jobs, is_job_timeout, select_failed_jobs
from cicd_doctor.core.models import (
    CachedDiagnosis,
    CheckOutput,
    CommentTarget,
    DiagnoseRunPayload,
    DiagnosisReport,
    FailedJob,
    Failure,
    FailureStatus,
    JobRef,
    LoadedConfig,
    Publication,
    PublicationKey,
    PublishedUpgrade,
    RawLog,
    RepoRef,
    ResolvedBy,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    SimilarFailure,
    StoredFailure,
    WorkflowJob,
    WorkflowRun,
    failure_id_for,
)
from cicd_doctor.core.outcomes import decide_late
from cicd_doctor.core.policy import PolicyInput, decide
from cicd_doctor.core.ports import (
    Clock,
    Embedder,
    FailureStore,
    InstallationStore,
    LogSource,
    PullRequestSource,
    RepoConfigSource,
    Reporter,
    TokenCounter,
)
from cicd_doctor.core.render import (
    render_check,
    render_comment,
    render_could_not_diagnose,
)
from cicd_doctor.core.repo_config import DEFAULT_LOADED, effective_llm, ignores
from cicd_doctor.core.usecases.report import build_report, find_similar
from cicd_doctor.core.usecases.rows import ensure_rows

logger = logging.getLogger(__name__)

PERMISSION_STATUS = 403


@dataclass(frozen=True)
class DiagnoseConfig:
    """What the use case takes from the settings."""

    llm_enabled: bool = True  # LLM_ENABLED
    max_jobs: int = 3  # LOG_MAX_JOBS
    max_bytes: int = 20_971_520  # LOG_MAX_BYTES, per job
    excerpt_max_tokens: int = 6000  # EXCERPT_MAX_TOKENS, shared by the jobs of a run
    logs_grace: timedelta = timedelta(minutes=10)  # a 404 this soon after the run is transient
    logs_retry_after: timedelta = timedelta(seconds=30)
    concurrency: int = 3  # jobs diagnosed at the same time
    cache_ttl_days: int = 7  # CACHE_TTL_DAYS; 0 turns the exact-signature cache off (D-81)
    similar_k: int = 3  # SIMILAR_K: past failures listed per job; 0 turns the search off (D-82)
    similar_min_score: float = 0.85  # SIMILAR_MIN_SCORE (cosine similarity, D-80)


@dataclass(frozen=True)
class _Examined:
    """What the gate of a cancelled run read: the jobs and the annotations of the cancelled ones."""

    jobs: list[WorkflowJob]
    annotations: dict[int, list[str]]


class DiagnoseFailedRun:
    def __init__(
        self,
        *,
        logs: LogSource,
        pulls: PullRequestSource,
        reporter: Reporter,
        store: FailureStore,
        installations: InstallationStore,
        counter: TokenCounter,
        diagnose: DiagnoseJob,
        clock: Clock,
        embedder: Embedder | None = None,
        repo_config: RepoConfigSource | None = None,
        config: DiagnoseConfig | None = None,
    ) -> None:
        self._logs = logs
        self._pulls = pulls
        self._reporter = reporter
        self._store = store
        self._installations = installations
        self._counter = counter
        self._diagnose = diagnose
        self._clock = clock
        self._embedder = embedder
        self._repo_config = repo_config
        self._config = config or DiagnoseConfig()

    async def run(self, payload: DiagnoseRunPayload) -> None:
        """Diagnose one run attempt and publish it; safe to call again after any failure.

        Raises ``TransientError`` (retry the job) or ``PermanentError``. A run with no failed job
        is not an error: the neutral check is published and the call returns.
        """
        run = payload.run
        check_key = PublicationKey.check(run.repo_id, run.run_id)
        examined: _Examined | None = None
        if payload.conclusion == "cancelled":
            # Nothing is written before the decision: a manual cancellation stays silent (D-73).
            if await self._superseded(check_key, run):
                return
            examined = await self._examine_cancelled(run)
            if examined is None:
                return
        await self._ensure_rows(payload)
        loaded = await self._load_config(payload)
        if ignores(loaded, run.workflow_name):
            logger.info(
                "run_id=%d attempt=%d: the workflow is in ignore_workflows, nothing to do",
                run.run_id,
                run.run_attempt,
            )
            return
        if await self._superseded(check_key, run):
            return
        explain = payload.trigger == "explain"
        check_id = await self._explained_check(run, check_key) if explain else None
        if check_id is None:
            check_id = await self._start_check(run, check_key)
        if check_id is None:
            return
        record = await self._store.run_analysis(run.run_id, run.run_attempt)
        if record is None:
            record = await self._analyze(payload, loaded, examined)
        elif explain:
            record = await self._upgrade(payload, loaded, check_key)
        record = await self._resolve_late(record)
        report = await self._report(payload, record, loaded)
        if not await self._complete_check(payload, record, report, check_id, check_key):
            return
        await self._comment(payload, record, report, loaded)

    async def publish_could_not_diagnose(
        self,
        run: RunRef,
        reason: str,
        completed_at: datetime | None = None,
        *,
        only_if_started: bool = False,
    ) -> bool:
        """Complete the check run of ``run`` as "Could not diagnose" (``COULD_NOT_DIAGNOSE``
        keys: ``logs_expired``, ``no_failed_job``, ``permission``, ``internal_error``).

        It writes nothing when a newer attempt already published the check run, and returns
        whether it published. The handler uses it for the errors the worker gives up on (D-74).
        With ``only_if_started`` it also writes nothing unless this attempt already started the
        check run: a cancelled run that failed before the timeout decision must stay silent
        (D-73).
        """
        key = PublicationKey.check(run.repo_id, run.run_id)
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if _newer(existing, run):
                return False
            if only_if_started and (
                existing is None or (existing.run_id, existing.attempt) != _id_of(run)
            ):
                return False
            check_id = (
                existing.external_id if existing else await self._reporter.start_check(run, None)
            )
            await self._reporter.complete_check(run, check_id, render_could_not_diagnose(reason))
            saved = await self._store.save_publication(
                Publication(
                    key=key, external_id=check_id, run_id=run.run_id, attempt=run.run_attempt
                )
            )
        if saved:
            self._log_published("check", run, completed_at)
        return saved

    # --- rows -------------------------------------------------------------------------------

    async def _ensure_rows(self, payload: DiagnoseRunPayload) -> None:
        """Make the installation and repository rows exist (D-75), before anything else.

        The plan asked for this after the installation token works (proof that the repository is
        still installed), but the credential belongs to the adapters, so it is the first thing
        the use case does. A removed repository or a deleted installation still has a row.
        """
        await ensure_rows(
            self._installations,
            payload.run,
            account_login=payload.account_login,
            account_type=payload.account_type,
            private=payload.private,
            default_branch=payload.default_branch,
        )

    async def _load_config(self, payload: DiagnoseRunPayload) -> LoadedConfig:
        """The repository configuration, read once per call (D-90). A transient error propagates
        (the job retries); without a source every repository uses the defaults."""
        if self._repo_config is None:
            return DEFAULT_LOADED
        run = payload.run
        return await self._repo_config.load(
            RepoRef(
                installation_id=run.installation_id,
                repo_id=run.repo_id,
                full_name=run.repo_full_name,
                default_branch=payload.default_branch,
            )
        )

    # --- cancelled runs ---------------------------------------------------------------------

    async def _examine_cancelled(self, run: RunRef) -> _Examined | None:
        """Decide whether a cancelled run is a job-level timeout (D-73); None means stay silent.

        Reads the jobs of the attempt and the annotations of each cancelled job (at most
        ``MAX_ANNOTATION_JOBS``). It is skipped when the analysis of this attempt is already
        stored: that proves an earlier call passed the gate. Errors propagate: a retry decides
        again.
        """
        if await self._store.run_analysis(run.run_id, run.run_attempt) is not None:
            return _Examined(jobs=[], annotations={})
        jobs = await self._logs.jobs(run)
        annotations: dict[int, list[str]] = {}
        for job in cancelled_jobs(jobs):
            annotations[job.job_id] = await self._logs.job_annotations(
                JobRef(run=run, job_id=job.job_id, job_name=job.name)
            )
            if is_job_timeout(annotations[job.job_id]):
                return _Examined(jobs=jobs, annotations=annotations)
        logger.info(
            "run_id=%d attempt=%d: cancelled without a job timeout, nothing to publish",
            run.run_id,
            run.run_attempt,
        )
        return None

    # --- a success that came first (D-83) ---------------------------------------------------

    async def _resolve_late(self, record: RunAnalysisRecord) -> RunAnalysisRecord:
        """Resolve the failures of this attempt that a recorded success already fixed.

        The checkpoint is stored before this reads the successes, and a success is stored before
        it reads the open failures, so whichever arrives second sees the other (D-83). One query
        per attempt: its failures share workflow and branch. The record is read again when
        something changed, so the report and the comment already show the resolved state.
        """
        if not any(item.failure.status is FailureStatus.OPEN for item in record.items):
            return record
        run = record.items[0].failure.job.job.run
        successes = await self._store.later_successes(
            run.repo_id, run.workflow_id, run.branch_key, run.run_id, run.run_attempt
        )
        if not successes:
            return record
        groups: dict[tuple[FailureStatus, str | None, ResolvedBy | None], list[UUID]] = {}
        for item in record.items:
            resolution = decide_late(item.failure, successes)
            if resolution is not None:
                key = (resolution.status, resolution.fix_commit_sha, resolution.resolved_by)
                groups.setdefault(key, []).append(resolution.failure_id)
        if not groups:
            return record
        for (status, fix_commit_sha, by), failure_ids in groups.items():
            await self._store.resolve(failure_ids, status, fix_commit_sha, by)
        return await self._store.run_analysis(run.run_id, run.run_attempt) or record

    # --- the check run ----------------------------------------------------------------------

    async def _superseded(self, key: PublicationKey, run: RunRef) -> bool:
        if _newer(await self._store.publication(key), run):
            logger.info(
                "run_id=%d attempt=%d: a newer attempt already published, nothing to do",
                run.run_id,
                run.run_attempt,
            )
            return True
        return False

    async def _start_check(self, run: RunRef, key: PublicationKey) -> int | None:
        """Put the check run in progress and store its id at once; None if superseded."""
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if _newer(existing, run):
                return None
            check_id = await self._reporter.start_check(
                run, existing.external_id if existing else None
            )
            saved = await self._store.save_publication(
                Publication(
                    key=key, external_id=check_id, run_id=run.run_id, attempt=run.run_attempt
                )
            )
        return check_id if saved else None

    async def _explained_check(self, run: RunRef, key: PublicationKey) -> int | None:
        """The check run this very attempt already published, for ``explain`` to edit in place
        (D-95). It is not started again: it is ``completed`` and GitHub would ignore it. None
        when this attempt has no check run yet (or only an older one): the normal start follows.
        """
        existing = await self._store.publication(key)
        if existing is not None and (existing.run_id, existing.attempt) == _id_of(run):
            return existing.external_id
        return None

    async def _upgrade(
        self, payload: DiagnoseRunPayload, loaded: LoadedConfig, key: PublicationKey
    ) -> RunAnalysisRecord:
        """Improve the published diagnosis of an analysed attempt with the LLM (D-95).

        Only the failures whose published diagnosis is still a rules one are analysed again, from
        what is stored: no log is downloaded and nothing is cut again. The whole step runs under
        the lock of the check run, and the record is read again under it, so two explains of the
        same attempt call the model once: the second finds the upgraded diagnosis and does
        nothing. A newer attempt that already published makes the step a no-op. The LLM, the
        budget and the cache are used exactly as in the automatic flow, so a failure whose call
        falls back (budget, error, ``llm: off``) keeps its rules diagnosis and its reason.
        """
        run = payload.run
        async with self._store.publication_lock(key):
            if _newer(await self._store.publication(key), run):
                return await self._stored(run)
            record = await self._stored(run)
            pending = [item for item in record.items if item.published.source == "rules"]
            if not pending:
                return record
            analyses = [analysis_from_stored(item.failure) for item in pending]
            embeddings = [item.failure.embedding for item in pending]
            results = await self._diagnose_all(run, analyses, embeddings, loaded, explain=True)
            upgrades = [
                PublishedUpgrade(
                    failure_id=failure_id_for(run.run_id, run.run_attempt, analysis.job.job.job_id),
                    published=result.published,
                    usage=result.usage,
                    cached_from=result.cached_from,
                )
                for analysis, result in results
                if result.published.source != "rules"
            ]
            if not upgrades:
                return record
            return await self._store.upgrade_published(run.run_id, run.run_attempt, upgrades)

    async def _stored(self, run: RunRef) -> RunAnalysisRecord:
        record = await self._store.run_analysis(run.run_id, run.run_attempt)
        if record is None:  # an analysis is never removed while its job runs
            raise TransientError("the analysis of this attempt is gone; the job starts again")
        return record

    async def _complete_check(
        self,
        payload: DiagnoseRunPayload,
        record: RunAnalysisRecord,
        report: DiagnosisReport,
        check_id: int,
        key: PublicationKey,
    ) -> bool:
        run = report.run
        output: CheckOutput = (
            render_check(report) if record.items else render_could_not_diagnose("no_failed_job")
        )
        async with self._store.publication_lock(key):
            if _newer(await self._store.publication(key), run):
                return False
            await self._reporter.complete_check(run, check_id, output)
            saved = await self._store.save_publication(
                Publication(
                    key=key, external_id=check_id, run_id=run.run_id, attempt=run.run_attempt
                )
            )
        if saved:
            self._log_published("check", run, payload.completed_at)
        return saved

    # --- the comment ------------------------------------------------------------------------

    async def _comment(
        self,
        payload: DiagnoseRunPayload,
        record: RunAnalysisRecord,
        report: DiagnosisReport,
        loaded: LoadedConfig,
    ) -> None:
        run = report.run
        pr_number = _pr_number(payload, record)
        if pr_number is None or not loaded.config.comment or not record.items:
            return
        key = PublicationKey.comment(run.repo_id, pr_number, run.workflow_id)
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if _newer(existing, run):
                return
            # A success that resolved these failures after the report was built has found no
            # comment to rewrite (this lock keeps it out until we are done), so what is written
            # must come from the store as it is now (D-84).
            fresh = await self._store.run_analysis(run.run_id, run.run_attempt)
            if fresh is not None and _statuses(fresh) != _statuses(record):
                report = await self._report(payload, fresh, loaded)
            comment_id = await self._reporter.upsert_comment(
                CommentTarget(run=run, pr_number=pr_number),
                render_comment(report),
                existing.external_id if existing else None,
            )
            saved = await self._store.save_publication(
                Publication(
                    key=key, external_id=comment_id, run_id=run.run_id, attempt=run.run_attempt
                )
            )
        if saved:
            self._log_published("comment", run, payload.completed_at)

    # --- the analysis -----------------------------------------------------------------------

    async def _analyze(
        self, payload: DiagnoseRunPayload, loaded: LoadedConfig, examined: _Examined | None = None
    ) -> RunAnalysisRecord:
        """Jobs, logs, excerpts, rules and the LLM; store the result and return what was stored
        (the first writer wins, so a concurrent attempt of the same job gives the same record)."""
        try:
            run, logs = await self._read_logs(payload.run, examined)
        except LogsUnavailable as exc:
            await self._logs_unavailable(payload, exc)
        except PermanentError as exc:
            if getattr(exc, "status_code", None) == PERMISSION_STATUS:
                await self.publish_could_not_diagnose(
                    payload.run, "permission", payload.completed_at
                )
            raise
        analyses = analyze_jobs(
            logs,
            self._counter,
            ExcerptConfig(
                max_tokens=self._config.excerpt_max_tokens, max_bytes=self._config.max_bytes
            ),
        )
        embeddings = await self._embed(analyses)
        results = await self._diagnose_all(
            run, analyses, embeddings, loaded, explain=payload.trigger == "explain"
        )
        record = RunAnalysisRecord(
            run=_workflow_run(run),
            attempt=RunAttempt(
                run_id=run.run_id,
                attempt=run.run_attempt,
                conclusion=payload.conclusion,
                completed_at=payload.completed_at,
            ),
            items=[
                _stored(run, analysis, result, embedding)
                for (analysis, result), embedding in zip(results, embeddings, strict=True)
            ],
        )
        return await self._store.save_run_analysis(record)

    async def _embed(self, analyses: list[JobAnalysis]) -> list[list[float] | None]:
        """One embedding per signature text, in one batch and off the event loop (D-77).

        Any error leaves every embedding ``None``: the failure is stored without a vector and the
        diagnosis goes on. Only the class of the error is logged, never a text.
        """
        missing: list[list[float] | None] = [None] * len(analyses)
        if self._embedder is None or not analyses:
            return missing
        texts = [analysis.signature.text for analysis in analyses]
        try:
            vectors = await asyncio.to_thread(self._embedder.embed, texts)
        except Exception as exc:
            logger.warning(
                "no embeddings for this run (%s): the failures are stored without one",
                type(exc).__name__,
            )
            return missing
        if len(vectors) != len(texts) or any(len(v) != self._embedder.dims for v in vectors):
            logger.warning("no embeddings for this run: the embedder returned the wrong shape")
            return missing
        return list(vectors)

    async def _read_logs(
        self, run: RunRef, examined: _Examined | None = None
    ) -> tuple[RunRef, list[tuple[FailedJob, RawLog]]]:
        """The failed jobs of the attempt with their logs, and the run with its pull request.
        A cancelled run brings the jobs and annotations its gate already read (D-73).

        The pull request is looked up only when there is something to diagnose and before any
        log is downloaded, so a lookup that fails wastes nothing. Logs are downloaded one after
        the other (at most ``max_jobs``).
        """
        if examined is not None:
            jobs, annotations = examined.jobs, examined.annotations
        else:
            jobs, annotations = await self._logs.jobs(run), {}
        failed = select_failed_jobs(run, jobs, self._config.max_jobs, annotations)
        if failed and run.pr_number is None:
            run = run.model_copy(update={"pr_number": await self._pulls.find(run)})
            failed = select_failed_jobs(run, jobs, self._config.max_jobs, annotations)
        logs = [(job, await self._logs.job_log(job.job, self._config.max_bytes)) for job in failed]
        return run, logs

    async def _logs_unavailable(
        self, payload: DiagnoseRunPayload, exc: LogsUnavailable
    ) -> NoReturn:
        age = self._clock.now() - payload.completed_at
        if exc.status_code == 404 and age < self._config.logs_grace:
            raise TransientError(
                "the job log is not available yet", retry_after=self._config.logs_retry_after
            ) from exc
        await self.publish_could_not_diagnose(payload.run, "logs_expired", payload.completed_at)
        raise LogsUnavailable(
            "the job log is no longer available", status_code=exc.status_code
        ) from exc

    async def _diagnose_all(
        self,
        run: RunRef,
        analyses: list[JobAnalysis],
        embeddings: Sequence[Sequence[float] | None],
        loaded: LoadedConfig,
        *,
        explain: bool = False,
    ) -> list[tuple[JobAnalysis, JobDiagnosis]]:
        context = DiagnosisContext(
            installation_id=run.installation_id,
            is_fork=run.is_fork,
            llm_enabled=self._config.llm_enabled,
            repo_llm=effective_llm(loaded),
            explain=explain,
        )
        slots = asyncio.Semaphore(self._config.concurrency)

        async def one(analysis: JobAnalysis, embedding: Sequence[float] | None) -> JobDiagnosis:
            async with slots:
                cached = await self._cached(run, analysis, context)
                # D-94: nothing of the history reaches the prompt of a fork run.
                similar = [] if run.is_fork else await self._similar(run, embedding)
                job_context = replace(context, cached=cached, similar=similar)
                return await self._diagnose.run(analysis, job_context)

        results = await asyncio.gather(
            *(
                one(analysis, embedding)
                for analysis, embedding in zip(analyses, embeddings, strict=True)
            )
        )
        return list(zip(analyses, results, strict=True))

    async def _cached(
        self, run: RunRef, analysis: JobAnalysis, context: DiagnosisContext
    ) -> CachedDiagnosis | None:
        """The exact-signature cache of this repository for one job (D-81), or None.

        The store is asked only when the policy, without a cache, would call the LLM: a fork,
        ``llm: off`` and ``LLM_ENABLED=false`` never read it (§9.4 keeps its order: the fork rule
        comes before the cache). The jobs of one attempt are looked up at the same time, before
        anything of this attempt is stored, so a matrix whose jobs share a signature calls the
        model once per job: the cache serves later runs, not siblings.
        """
        if self._config.cache_ttl_days <= 0 or run.is_fork:
            return None  # a fork run neither reads nor writes the cache (D-94)
        if decide(_policy_without_cache(context)).action != "llm":
            return None
        hit = await self._store.cached_diagnosis(
            run.repo_id,
            analysis.signature.hash,
            timedelta(days=self._config.cache_ttl_days),
            PROMPT_VERSION,
        )
        if hit is None:
            return None
        reused = reuse_cached(hit.diagnosis, analysis.excerpt)
        if reused is None:
            return None
        return CachedDiagnosis(diagnosis=reused, origin_id=hit.origin_id)

    async def _similar(
        self, run: RunRef, embedding: Sequence[float] | None
    ) -> list[SimilarFailure]:
        """The past failures of this repository that look like one job (D-82), best first.

        Asked before the LLM, so the prompt can use them, and again when the report is built: they
        are not part of the checkpoint. A job with no embedding (the embedder was not ready) or
        ``SIMILAR_K=0`` has none. The store never returns a fork, a failure of this run or one
        of another repository (D-79). A database error propagates: the job is retried. The
        result is shown for a fork run too, but its policy never sends anything to the model.
        """
        return await find_similar(
            self._store,
            run.repo_id,
            embedding,
            k=self._config.similar_k,
            min_score=self._config.similar_min_score,
            exclude_run_id=run.run_id,
        )

    # --- the report -------------------------------------------------------------------------

    async def _report(
        self, payload: DiagnoseRunPayload, record: RunAnalysisRecord, loaded: LoadedConfig
    ) -> DiagnosisReport:
        """The report of a stored analysis. The run carries the pull request that was found."""
        run = payload.run.model_copy(update={"pr_number": _pr_number(payload, record)})
        return await build_report(
            self._store,
            run,
            record,
            similar_k=self._config.similar_k,
            similar_min_score=self._config.similar_min_score,
            config_problems=loaded.problems,
        )

    def _log_published(self, kind: str, run: RunRef, completed_at: datetime | None) -> None:
        """One line per publication, to measure NFR-02 (failure to published) later."""
        seconds = (
            f"{(self._clock.now() - completed_at).total_seconds():.1f}" if completed_at else "n/a"
        )
        logger.info(
            "published %s run_id=%d attempt=%d seconds_since_completed=%s",
            kind,
            run.run_id,
            run.run_attempt,
            seconds,
        )


def _policy_without_cache(context: DiagnosisContext) -> PolicyInput:
    """The policy input of a job, with no cache hit (what §9.4 says before the cache rule)."""
    return PolicyInput(
        llm_enabled=context.llm_enabled,
        repo_llm=context.repo_llm,
        is_fork=context.is_fork,
        explain=context.explain,
        cache_hit=False,
    )


def _pr_number(payload: DiagnoseRunPayload, record: RunAnalysisRecord) -> int | None:
    """The pull request of the comment: the one stored with the run or, when the run has none
    (a fork run is not linked to its pull request by GitHub), the one of the command (D-95)."""
    if record.run.pr_number is not None:
        return record.run.pr_number
    return payload.pr_number if payload.trigger == "explain" else None


def _statuses(record: RunAnalysisRecord) -> list[tuple[FailureStatus, str | None]]:
    return [(item.failure.status, item.failure.fix_commit_sha) for item in record.items]


def _newer(existing: Publication | None, run: RunRef) -> bool:
    """True when a later attempt (or run) already published this output (D-70)."""
    return existing is not None and (existing.run_id, existing.attempt) > (
        run.run_id,
        run.run_attempt,
    )


def _id_of(run: RunRef) -> tuple[int, int]:
    return (run.run_id, run.run_attempt)


def _workflow_run(run: RunRef) -> WorkflowRun:
    return WorkflowRun(
        id=run.run_id,
        repo_id=run.repo_id,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow_name,
        head_sha=run.head_sha,
        head_branch=run.head_branch,
        head_repo_full_name=run.head_repo_full_name,
        event=run.event,
        is_fork=run.is_fork,
        pr_number=run.pr_number,
    )


def _stored(
    run: RunRef, analysis: JobAnalysis, result: JobDiagnosis, embedding: list[float] | None
) -> StoredFailure:
    job_id = analysis.job.job.job_id
    return StoredFailure(
        failure=Failure(
            id=failure_id_for(run.run_id, run.run_attempt, job_id),
            job=analysis.job,
            excerpt=analysis.excerpt,
            signature=analysis.signature,
            embedding=embedding,
            status=FailureStatus.OPEN,
            fix_commit_sha=None,
        ),
        published=result.published,
        rules=result.rules,
        usage=result.usage,
        cached_from=result.cached_from,
    )
