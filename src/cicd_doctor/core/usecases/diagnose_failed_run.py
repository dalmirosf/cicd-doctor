"""Use case: diagnose a failed run attempt and publish it (SPEC §7.1, D-70, D-71, D-75).

The diagnosis of an attempt is computed once and published as many times as it takes, always from
what is stored:

1. a guard: when a newer attempt already published the check run, there is nothing to do;
2. the check run goes ``in_progress`` and its id is stored right away (under the publication lock);
3. the stored analysis of the attempt, or else jobs -> logs -> excerpts and rules -> the LLM per
   job -> ``save_run_analysis``. That commit is the checkpoint: the LLM is only ever called before
   it, so a retry after it downloads no log, calls no LLM and reserves no budget;
4. the check run is completed from the stored analysis (under the lock, guarded again);
5. the sticky comment is upserted from the stored analysis (under the lock of its own key).

Steps 2, 4 and 5 are idempotent, so a failure while publishing is retried by running the use case
again. Nothing here imports an adapter: the ports and the small ``DiagnoseConfig`` are injected.
Logs, excerpts and the text of a diagnosis are never logged.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from cicd_doctor.core.analysis import JobAnalysis, analyze_jobs
from cicd_doctor.core.diagnosis.merge import baseline_to_show
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext, JobDiagnosis
from cicd_doctor.core.errors import LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.logs import ExcerptConfig
from cicd_doctor.core.logs.select import select_failed_jobs
from cicd_doctor.core.models import (
    CheckOutput,
    CommentTarget,
    DiagnoseRunPayload,
    DiagnosisReport,
    FailedJob,
    Failure,
    FailureStatus,
    Installation,
    Publication,
    PublicationKey,
    RawLog,
    RepoConfig,
    Repository,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    StoredFailure,
    WorkflowRun,
    failure_id_for,
)
from cicd_doctor.core.ports import (
    Clock,
    FailureStore,
    InstallationStore,
    LogSource,
    PullRequestSource,
    Reporter,
    TokenCounter,
)
from cicd_doctor.core.render import (
    render_check,
    render_comment,
    render_could_not_diagnose,
)

logger = logging.getLogger(__name__)

# P1c has no ``RepoConfigSource``: every repository uses the defaults (comment on, llm auto).
# Reading ``.github/cicd-doctor.yml`` needs ``contents: read`` and comes later (D-26).
DEFAULT_REPO_CONFIG = RepoConfig()
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
        self._config = config or DiagnoseConfig()

    async def run(self, payload: DiagnoseRunPayload) -> None:
        """Diagnose one run attempt and publish it; safe to call again after any failure.

        Raises ``TransientError`` (retry the job) or ``PermanentError``. A run with no failed job
        is not an error: the neutral check is published and the call returns.
        """
        run = payload.run
        await self._ensure_rows(payload)
        check_key = PublicationKey.check(run.repo_id, run.run_id)
        if await self._superseded(check_key, run):
            return
        check_id = await self._start_check(run, check_key)
        if check_id is None:
            return
        record = await self._store.run_analysis(run.run_id, run.run_attempt)
        if record is None:
            record = await self._analyze(payload)
        report = self._report(payload, record)
        if not await self._complete_check(payload, record, report, check_id, check_key):
            return
        await self._comment(payload, record, report)

    async def publish_could_not_diagnose(
        self, run: RunRef, reason: str, completed_at: datetime | None = None
    ) -> bool:
        """Complete the check run of ``run`` as "Could not diagnose" (``COULD_NOT_DIAGNOSE``
        keys: ``logs_expired``, ``no_failed_job``, ``permission``, ``internal_error``).

        It writes nothing when a newer attempt already published the check run, and returns
        whether it published. The handler uses it for the errors the worker gives up on (D-74).
        """
        key = PublicationKey.check(run.repo_id, run.run_id)
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if _newer(existing, run):
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
        run = payload.run
        if payload.account_login and payload.account_type:
            await self._installations.ensure_installation(
                Installation(
                    id=run.installation_id,
                    account_login=payload.account_login,
                    account_type=payload.account_type,
                )
            )
        elif await self._installations.installation(run.installation_id) is None:
            # A job queued before P1c carries no account, and nothing tracks this installation.
            raise PermanentError(
                f"installation {run.installation_id} is unknown and the job has no account"
            )
        await self._installations.save_repositories(
            [
                Repository(
                    id=run.repo_id,
                    installation_id=run.installation_id,
                    full_name=run.repo_full_name,
                    private=payload.private,
                    default_branch=payload.default_branch,
                )
            ]
        )

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
        self, payload: DiagnoseRunPayload, record: RunAnalysisRecord, report: DiagnosisReport
    ) -> None:
        run = report.run
        pr_number = record.run.pr_number
        if pr_number is None or not DEFAULT_REPO_CONFIG.comment or not record.items:
            return
        key = PublicationKey.comment(run.repo_id, pr_number, run.workflow_id)
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if _newer(existing, run):
                return
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

    async def _analyze(self, payload: DiagnoseRunPayload) -> RunAnalysisRecord:
        """Jobs, logs, excerpts, rules and the LLM; store the result and return what was stored
        (the first writer wins, so a concurrent attempt of the same job gives the same record)."""
        try:
            run, logs = await self._read_logs(payload.run)
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
        results = await self._diagnose_all(run, analyses)
        record = RunAnalysisRecord(
            run=_workflow_run(run),
            attempt=RunAttempt(
                run_id=run.run_id,
                attempt=run.run_attempt,
                conclusion=payload.conclusion,
                completed_at=payload.completed_at,
            ),
            items=[_stored(run, analysis, result) for analysis, result in results],
        )
        return await self._store.save_run_analysis(record)

    async def _read_logs(self, run: RunRef) -> tuple[RunRef, list[tuple[FailedJob, RawLog]]]:
        """The failed jobs of the attempt with their logs, and the run with its pull request.

        The pull request is looked up only when there is something to diagnose and before any
        log is downloaded, so a lookup that fails wastes nothing. Logs are downloaded one after
        the other (at most ``max_jobs``).
        """
        jobs = await self._logs.jobs(run)
        failed = select_failed_jobs(run, jobs, self._config.max_jobs)
        if failed and run.pr_number is None:
            run = run.model_copy(update={"pr_number": await self._pulls.find(run)})
            failed = select_failed_jobs(run, jobs, self._config.max_jobs)
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
        self, run: RunRef, analyses: list[JobAnalysis]
    ) -> list[tuple[JobAnalysis, JobDiagnosis]]:
        context = DiagnosisContext(
            installation_id=run.installation_id,
            is_fork=run.is_fork,
            llm_enabled=self._config.llm_enabled,
            repo_llm=DEFAULT_REPO_CONFIG.llm,
        )
        slots = asyncio.Semaphore(self._config.concurrency)

        async def one(analysis: JobAnalysis) -> JobDiagnosis:
            async with slots:
                return await self._diagnose.run(analysis, context)

        results = await asyncio.gather(*(one(analysis) for analysis in analyses))
        return list(zip(analyses, results, strict=True))

    # --- the report -------------------------------------------------------------------------

    @staticmethod
    def _report(payload: DiagnoseRunPayload, record: RunAnalysisRecord) -> DiagnosisReport:
        """The report of a stored analysis. The run carries the pull request that was found."""
        run = payload.run.model_copy(update={"pr_number": record.run.pr_number})
        baselines = {
            item.failure.id: baseline
            for item in record.items
            if (baseline := baseline_to_show(item.rules, item.published)) is not None
        }
        return DiagnosisReport(
            run=run,
            items=[(item.failure, item.published) for item in record.items],
            rules_baseline=baselines,
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


def _newer(existing: Publication | None, run: RunRef) -> bool:
    """True when a later attempt (or run) already published this output (D-70)."""
    return existing is not None and (existing.run_id, existing.attempt) > (
        run.run_id,
        run.run_attempt,
    )


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


def _stored(run: RunRef, analysis: JobAnalysis, result: JobDiagnosis) -> StoredFailure:
    job_id = analysis.job.job.job_id
    return StoredFailure(
        failure=Failure(
            id=failure_id_for(run.run_id, run.run_attempt, job_id),
            job=analysis.job,
            excerpt=analysis.excerpt,
            signature=analysis.signature,
            embedding=None,  # P2
            status=FailureStatus.OPEN,
            fix_commit_sha=None,
        ),
        published=result.published,
        rules=result.rules,
        usage=result.usage,
    )
