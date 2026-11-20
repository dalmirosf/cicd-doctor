"""Use case: the ``/cicd-doctor explain`` command (FR-19, D-92, D-93, D-96).

The webhook already checked who wrote the comment and what it says (D-92), without calling GitHub.
Here the worker:

1. applies the limit of one accepted command per PR per window (D-93) and acknowledges an accepted
   one with a 👀 reaction (a refused command ends silently: no reaction, no comment, so the policy
   is not revealed, §9.7);
2. reads the pull request and ends if it is closed or merged;
3. ends with a 😕 when the LLM is unavailable for this repository (``LLM_ENABLED=false``,
   ``llm: off`` or an invalid configuration file) or when no run is worth explaining;
4. otherwise lists the runs of the PR's head commit and enqueues one child ``diagnose_run`` job per
   selected run (``select_runs``), which upgrades the published diagnosis in place (D-94, D-95).

There is no second reaction when the children are queued: the check runs and the comments that
change are the answer. Every step is safe to repeat: the reactions are idempotent, and a child's
dedupe key names the command, so a retry of this job adds no second child.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Literal

from cicd_doctor.core.errors import CommentGone
from cicd_doctor.core.models import (
    CommentRef,
    DiagnoseRunPayload,
    ExplainPayload,
    JobKind,
    NewJob,
    PullRequestInfo,
    RepoRef,
    RunSummary,
    explain_run_dedupe_key,
)
from cicd_doctor.core.ports import (
    Clock,
    CommandLog,
    JobQueue,
    PullRequestSource,
    RepoConfigSource,
    Reporter,
    RunSource,
)
from cicd_doctor.core.repo_config import effective_llm, ignores

logger = logging.getLogger(__name__)

ACKNOWLEDGED = "eyes"
NOTHING_TO_EXPLAIN = "confused"
DEFAULT_WINDOW = timedelta(minutes=5)
DEFAULT_MAX_RUNS = 3
EXPLAINED_CONCLUSIONS = frozenset({"failure", "timed_out"})
CHILD_MAX_ATTEMPTS = 5


def select_runs(
    pull: PullRequestInfo, runs: Sequence[RunSummary], *, limit: int = DEFAULT_MAX_RUNS
) -> list[RunSummary]:
    """The runs ``explain`` diagnoses, newest first (D-96).

    Only a finished run that failed (``failure`` or ``timed_out``: ``action_required``,
    ``cancelled`` and the rest are not failures to explain, and a run still in progress is
    ignored) of the head commit the pull request has now, from the head repository it has now
    (never by the ``pull_requests`` of the run: a fork run has none). The latest run (greatest id)
    of each workflow, then the ``limit`` greatest ids overall, so the result does not depend on
    the order GitHub listed them in.
    """
    latest: dict[int, RunSummary] = {}
    for item in runs:
        if (
            item.status != "completed"
            or item.conclusion not in EXPLAINED_CONCLUSIONS
            or item.run.head_sha != pull.head_sha
            or item.head_repo_id != pull.head_repo_id
        ):
            continue
        current = latest.get(item.run.workflow_id)
        if current is None or item.run.run_id > current.run.run_id:
            latest[item.run.workflow_id] = item
    ordered = sorted(latest.values(), key=lambda item: item.run.run_id, reverse=True)
    return ordered[:limit]


class ExplainCommand:
    def __init__(
        self,
        commands: CommandLog,
        reporter: Reporter,
        pulls: PullRequestSource,
        runs: RunSource,
        repo_config: RepoConfigSource,
        queue: JobQueue,
        clock: Clock,
        *,
        window: timedelta = DEFAULT_WINDOW,
        max_runs: int = DEFAULT_MAX_RUNS,
        llm_enabled: bool = True,
    ) -> None:
        self._commands = commands
        self._reporter = reporter
        self._pulls = pulls
        self._runs = runs
        self._repo_config = repo_config
        self._queue = queue
        self._clock = clock
        self._window = window
        self._max_runs = max_runs
        self._llm_enabled = llm_enabled

    async def run(self, payload: ExplainPayload) -> bool:
        """``True`` when the command was accepted (whatever came of it), ``False`` when the limit
        refused it. Errors of the database or of GitHub propagate: the worker retries, and a retry
        of the same comment is accepted again."""
        accepted = await self._commands.claim(
            payload.repo_id,
            payload.pr_number,
            payload.comment_id,
            payload.actor_login,
            self._clock.now(),
            self._window,
        )
        if not accepted:
            logger.info(
                "repo_id=%d pr=%d: explain command refused by the rate limit",
                payload.repo_id,
                payload.pr_number,
            )
            return False
        comment = CommentRef(
            installation_id=payload.installation_id,
            repo_id=payload.repo_id,
            repo_full_name=payload.repo_full_name,
            comment_id=payload.comment_id,
        )
        if not await self._react(comment, ACKNOWLEDGED, payload):
            return True  # withdrawn: nothing to answer
        self._timing(payload, "acknowledged")
        repo = RepoRef(
            installation_id=payload.installation_id,
            repo_id=payload.repo_id,
            full_name=payload.repo_full_name,
            default_branch=payload.default_branch,
        )
        pull = await self._pulls.get(repo, payload.pr_number)
        if pull is None or pull.state != "open" or pull.merged:
            logger.info(
                "repo_id=%d pr=%d: explain ends, the pull request is closed or gone",
                payload.repo_id,
                payload.pr_number,
            )
            return True
        loaded = await self._repo_config.load(repo)
        if not self._llm_enabled or effective_llm(loaded) == "off":
            # The command exists to call the model: without it there is nothing to add.
            await self._react(comment, NOTHING_TO_EXPLAIN, payload)
            logger.info(
                "repo_id=%d pr=%d: explain ends, the LLM is off", payload.repo_id, pull.number
            )
            return True
        listed = await self._runs.runs_for_head(repo, pull.head_sha)
        selected = [
            item
            for item in select_runs(pull, listed, limit=len(listed))
            if not ignores(loaded, item.run.workflow_name)
        ][: self._max_runs]
        if not selected:
            await self._react(comment, NOTHING_TO_EXPLAIN, payload)
            logger.info(
                "repo_id=%d pr=%d: explain ends, no failed run to explain (%d listed)",
                payload.repo_id,
                pull.number,
                len(listed),
            )
            return True
        for item in selected:
            await self._queue.enqueue(_child(payload, item), None)
        self._timing(payload, "queued", children=len(selected))
        return True

    async def _react(self, comment: CommentRef, reaction: str, payload: ExplainPayload) -> bool:
        """``False`` when the command comment was deleted (the job did not fail: D-92)."""
        try:
            await self._reporter.react(comment, reaction)
        except CommentGone:
            logger.info(
                "repo_id=%d pr=%d: explain command deleted before the reaction",
                payload.repo_id,
                payload.pr_number,
            )
            return False
        return True

    def _timing(self, payload: ExplainPayload, step: str, **extra: int) -> None:
        """The 90 s goal of FR-19 is measured from the comment: ids and seconds only."""
        seconds = (self._clock.now() - payload.received_at).total_seconds()
        logger.info(
            "repo_id=%d pr=%d comment_id=%d: explain %s seconds_since_command=%.1f %s",
            payload.repo_id,
            payload.pr_number,
            payload.comment_id,
            step,
            seconds,
            " ".join(f"{key}={value}" for key, value in extra.items()),
        )


def _child(payload: ExplainPayload, item: RunSummary) -> NewJob:
    """The ``diagnose_run`` job of one run. It carries the command's ids, never its text."""
    conclusion: Literal["failure", "timed_out"] = (
        "timed_out" if item.conclusion == "timed_out" else "failure"
    )
    child = DiagnoseRunPayload(
        run=item.run,
        conclusion=conclusion,
        completed_at=item.updated_at,
        default_branch=payload.default_branch,
        private=payload.private,
        account_login=payload.account_login,
        account_type=payload.account_type,
        trigger="explain",
        pr_number=payload.pr_number,
        command_comment_id=payload.comment_id,
    )
    return NewJob(
        kind=JobKind.DIAGNOSE_RUN,
        payload=child.model_dump(mode="json"),
        dedupe_key=explain_run_dedupe_key(
            payload.comment_id, item.run.run_id, item.run.run_attempt
        ),
        max_attempts=CHILD_MAX_ATTEMPTS,
    )
