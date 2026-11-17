"""Use case: record a successful run attempt and resolve what it fixes (SPEC §7.2, D-83, D-84).

In this order, each step idempotent, so a failed job is retried by running it again:

1. the installation and repository rows exist (D-75);
2. the success is stored. This comes first: it is half of the order property of D-83 (the
   diagnosis of a late failure reads the successes after storing its own checkpoint);
3. the open failures of the branch are read and ``core.outcomes.decide`` says which ones this
   success resolves; ``resolve`` stores the status and the success that made it.

It calls no GitHub API: nothing here needs a token. The comment is not touched yet.
"""

import logging
from collections import defaultdict
from uuid import UUID

from cicd_doctor.core.models import (
    FailureStatus,
    RecordOutcomePayload,
    ResolvedBy,
    RunAttempt,
    WorkflowRun,
)
from cicd_doctor.core.outcomes import Outcome, decide
from cicd_doctor.core.ports import FailureStore, InstallationStore
from cicd_doctor.core.usecases.rows import ensure_rows

logger = logging.getLogger(__name__)


class RecordRunOutcome:
    def __init__(self, *, store: FailureStore, installations: InstallationStore) -> None:
        self._store = store
        self._installations = installations

    async def run(self, payload: RecordOutcomePayload) -> None:
        """Store a success and resolve the open failures it fixes. Safe to call again."""
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
        logger.info(
            "run_id=%d attempt=%d: success recorded, %d failure(s) resolved",
            run.run_id,
            run.run_attempt,
            len(resolutions),
        )
