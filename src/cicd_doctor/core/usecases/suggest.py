"""Publish the suggested changes of a diagnosis as review comments (SPEC §7.5, D-97).

A suggestion reaches here already checked against the diff the model was shown (``core.suggestions``
and ``finalize``). This step adds the checks that depend on the pull request **now**: it is open
and its head is still the commit of the run (the lines are valid only there). Then each
suggestion becomes one review comment, found again by its hidden marker so that a retry or a
second command edits instead of repeating. It runs under the lock of the sticky comment of that
(pull request, workflow), and it never raises for a problem of the suggestion itself: whatever
goes wrong, ``suggested_fix`` is already in the diagnosis as text and the report says why the
change was not posted. The texts of the diagnosis are never logged.
"""

import logging
from uuid import UUID

from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.errors import PermanentError, TransientError
from cicd_doctor.core.models import (
    CommentTarget,
    PublicationKey,
    RepoRef,
    ReviewSuggestion,
    RunAnalysisRecord,
    RunRef,
    StoredFailure,
)
from cicd_doctor.core.ports import FailureStore, PullRequestSource, Reporter
from cicd_doctor.core.render import render_review_comment

logger = logging.getLogger(__name__)

PERMISSION_STATUS = 403


class SuggestionPublisher:
    def __init__(
        self,
        *,
        pulls: PullRequestSource,
        reporter: Reporter,
        store: FailureStore,
        max_per_run: int = 3,
    ) -> None:
        self._pulls = pulls
        self._reporter = reporter
        self._store = store
        self._max = max_per_run

    async def publish(
        self, run: RunRef, repo: RepoRef, pr_number: int, record: RunAnalysisRecord
    ) -> dict[UUID, str]:
        """What became of each suggestion of ``record``: failure id -> a key of
        ``render.SUGGESTION_TEXT``. Failures without a suggestion are not listed.

        Only an ``llm`` diagnosis of the current prompt version counts: a cached one never has a
        suggestion, and one stored by an older prompt was not checked against a diff.
        """
        pending = [item for item in record.items if _publishable(item)]
        if not pending or self._max <= 0:
            return {}
        outcomes: dict[UUID, str] = {}
        key = PublicationKey.comment(run.repo_id, pr_number, run.workflow_id)
        async with self._store.publication_lock(key):
            problem = await self._pull_problem(repo, pr_number, run)
            for index, item in enumerate(pending):
                failure_id = item.failure.id
                if index >= self._max:
                    outcomes[failure_id] = "limit"
                elif problem is not None:
                    outcomes[failure_id] = problem
                else:
                    outcomes[failure_id] = await self._post(run, pr_number, item)
        return outcomes

    async def _pull_problem(self, repo: RepoRef, number: int, run: RunRef) -> str | None:
        """Why no suggestion can be posted on this pull request now, or None."""
        try:
            pull = await self._pulls.get(repo, number, for_explain=False)
        except (TransientError, PermanentError) as exc:
            logger.warning(
                "no suggestion posted: the pull request was not read (%s)", type(exc).__name__
            )
            return "unavailable"
        if pull is None or pull.state != "open" or pull.merged:
            return "closed"
        return None if pull.head_sha == run.head_sha else "head_moved"

    async def _post(self, run: RunRef, pr_number: int, item: StoredFailure) -> str:
        suggestion = item.published.suggestion
        assert suggestion is not None
        review = ReviewSuggestion(
            pr_number=pr_number,
            commit_id=run.head_sha,
            path=suggestion.path,
            start_line=suggestion.start_line,
            end_line=suggestion.end_line,
            body=render_review_comment(
                pr_number, item.failure.id, item.published.probable_cause, suggestion
            ),
        )
        try:
            posted = await self._reporter.post_suggestion(
                CommentTarget(run=run, pr_number=pr_number), review
            )
        except PermanentError as exc:
            logger.warning("suggestion not posted (%s)", type(exc).__name__)
            if getattr(exc, "status_code", None) == PERMISSION_STATUS:
                return "permission"
            return "unavailable"
        except TransientError as exc:
            logger.warning("suggestion not posted (%s)", type(exc).__name__)
            return "unavailable"
        return "posted" if posted else "outside_diff"


def _publishable(item: StoredFailure) -> bool:
    published = item.published
    return (
        published.source == "llm"
        and published.suggestion is not None
        and published.prompt_version == PROMPT_VERSION
    )
