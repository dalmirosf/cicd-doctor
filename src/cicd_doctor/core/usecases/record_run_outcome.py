"""Use case: record a success, resolve what it fixes and say so (SPEC §7.2, D-83, D-84).

In this order, each step idempotent, so a failed job is retried by running it again:

1. the installation and repository rows exist (D-75);
2. the success is stored. This comes first: it is half of the order property of D-83 (the
   diagnosis of a late failure reads the successes after storing its own checkpoint);
3. the open failures of the branch are read and ``core.outcomes.decide`` says which ones this
   success resolves; ``resolve`` stores the status and the success that made it. With nothing
   resolved by this success, now or in an earlier try, the call ends here, with no call to GitHub;
4. the sticky comment of each affected pull request is rewritten with the resolved state, under
   the publication lock of its key. The failures come from ``resolved_by``, not from step 3, so
   a retry after a crash between ``resolve`` and the ``PATCH`` still writes the comment.

Only the comment changes: the check run stays as the record of the diagnosis (D-84).
"""

import logging
from collections import defaultdict
from uuid import UUID

from cicd_doctor.core.models import (
    CommentTarget,
    Failure,
    FailureStatus,
    Publication,
    PublicationKey,
    RecordOutcomePayload,
    ResolvedBy,
    RunAnalysisRecord,
    RunAttempt,
    RunRef,
    WorkflowRun,
)
from cicd_doctor.core.outcomes import Outcome, decide
from cicd_doctor.core.ports import FailureStore, InstallationStore, Reporter
from cicd_doctor.core.render import render_comment
from cicd_doctor.core.usecases.report import build_report
from cicd_doctor.core.usecases.rows import ensure_rows

logger = logging.getLogger(__name__)


class RecordRunOutcome:
    def __init__(
        self,
        *,
        store: FailureStore,
        installations: InstallationStore,
        reporter: Reporter,
        comments_enabled: bool = True,
        similar_k: int = 3,
        similar_min_score: float = 0.85,
    ) -> None:
        self._store = store
        self._installations = installations
        self._reporter = reporter
        self._comments_enabled = comments_enabled
        self._similar_k = similar_k
        self._similar_min_score = similar_min_score

    async def run(self, payload: RecordOutcomePayload) -> None:
        """Store a success, resolve the open failures it fixes and rewrite their comments."""
        run = payload.run
        await ensure_rows(
            self._installations,
            run,
            account_login=payload.account_login,
            account_type=payload.account_type,
            private=payload.private,
            default_branch=payload.default_branch,
        )
        await self._store.record_success(
            WorkflowRun(
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
            ),
            RunAttempt(
                run_id=run.run_id,
                attempt=run.run_attempt,
                conclusion="success",
                completed_at=payload.completed_at,
            ),
        )
        await self._resolve(payload)
        resolved = await self._store.resolved_by(run.repo_id, run.run_id, run.run_attempt)
        logger.info(
            "run_id=%d attempt=%d: success recorded, %d failure(s) resolved by it",
            run.run_id,
            run.run_attempt,
            len(resolved),
        )
        if resolved and self._comments_enabled:
            await self._rewrite_comments(run, resolved)

    async def _resolve(self, payload: RecordOutcomePayload) -> None:
        run = payload.run
        open_failures = await self._store.open_failures(
            run.repo_id, run.workflow_id, run.branch_key
        )
        resolutions = decide(Outcome(success=run, completed_at=payload.completed_at), open_failures)
        groups: dict[tuple[FailureStatus, str | None], list[UUID]] = defaultdict(list)
        for resolution in resolutions:
            groups[(resolution.status, resolution.fix_commit_sha)].append(resolution.failure_id)
        by = ResolvedBy(run_id=run.run_id, attempt=run.run_attempt)
        for (status, fix_commit_sha), failure_ids in groups.items():
            await self._store.resolve(failure_ids, status, fix_commit_sha, by)

    # --- the comment (D-84) -----------------------------------------------------------------

    async def _rewrite_comments(self, success: RunRef, resolved: list[Failure]) -> None:
        """Rewrite the comment of every pull request that shows a failure this success resolved.

        The attempts of the resolved failures are grouped by the comment they belong to: one
        comment per (repository, pull request, workflow).
        """
        attempts: set[tuple[int, int]] = set()
        by_comment: dict[PublicationKey, set[tuple[int, int]]] = defaultdict(set)
        for failure in resolved:
            run = failure.job.job.run
            ids = (run.run_id, run.run_attempt)
            if ids in attempts:
                continue
            attempts.add(ids)
            record = await self._store.run_analysis(*ids)
            if record is None or record.run.pr_number is None:
                continue  # no pull request to write on
            key = PublicationKey.comment(
                record.run.repo_id, record.run.pr_number, record.run.workflow_id
            )
            by_comment[key].add(ids)
        for key, shown_attempts in by_comment.items():
            await self._rewrite(success, key, shown_attempts)

    async def _rewrite(
        self, success: RunRef, key: PublicationKey, resolved_attempts: set[tuple[int, int]]
    ) -> None:
        """Rewrite one comment, only when it still shows an attempt this success resolved."""
        assert key.pr_number is not None
        async with self._store.publication_lock(key):
            existing = await self._store.publication(key)
            if existing is None:
                return  # nothing is published yet: the diagnosis will write the resolved state
            shown = (existing.run_id, existing.attempt)
            if shown not in resolved_attempts:
                return  # the comment shows a later run or another attempt: not ours to change
            # Read under the lock: this is the state the comment must show now.
            record: RunAnalysisRecord | None = await self._store.run_analysis(*shown)
            if record is None or not record.items:
                return
            failing = record.items[0].failure.job.job.run
            run = failing.model_copy(update={"pr_number": record.run.pr_number})
            report = await build_report(
                self._store,
                run,
                record,
                similar_k=self._similar_k,
                similar_min_score=self._similar_min_score,
            )
            comment_id = await self._reporter.upsert_comment(
                CommentTarget(run=run, pr_number=key.pr_number),
                render_comment(report),
                existing.external_id,
            )
            saved = await self._store.save_publication(
                Publication(
                    key=key,
                    external_id=comment_id,
                    run_id=success.run_id,
                    attempt=success.run_attempt,
                )
            )
        if saved:
            logger.info(
                "published comment run_id=%d attempt=%d (resolved by it)",
                success.run_id,
                success.run_attempt,
            )
