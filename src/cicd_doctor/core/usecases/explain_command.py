"""Use case: the ``/cicd-doctor explain`` command, first half (FR-19, D-92, D-93).

The webhook already checked who wrote the comment and what it says (D-92), without calling GitHub.
Here the worker applies the limit of one accepted command per PR per window (D-93) and
acknowledges an accepted one with a 👀 reaction. A refused command ends silently: no reaction, no
comment, so the policy is not revealed and no noise is added (§9.7).

TODO(P3 d2): after the reaction, read the repository configuration (``llm: off`` and
``LLM_ENABLED=false`` still get the 👀 and skip the LLM, D-91) and diagnose the latest failed runs
of the PR. Until then an accepted command is only acknowledged.
"""

import logging
from datetime import timedelta

from cicd_doctor.core.errors import CommentGone
from cicd_doctor.core.models import CommentRef, ExplainPayload
from cicd_doctor.core.ports import Clock, CommandLog, Reporter

logger = logging.getLogger(__name__)

ACKNOWLEDGED = "eyes"
DEFAULT_WINDOW = timedelta(minutes=5)


class ExplainCommand:
    def __init__(
        self,
        commands: CommandLog,
        reporter: Reporter,
        clock: Clock,
        *,
        window: timedelta = DEFAULT_WINDOW,
    ) -> None:
        self._commands = commands
        self._reporter = reporter
        self._clock = clock
        self._window = window

    async def run(self, payload: ExplainPayload) -> bool:
        """``True`` when the command was accepted and acknowledged (or withdrawn after it was
        accepted), ``False`` when the limit refused it. Errors of the database or of GitHub
        propagate: the worker retries, and a retry of the same comment is accepted again."""
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
        try:
            await self._reporter.react(
                CommentRef(
                    installation_id=payload.installation_id,
                    repo_id=payload.repo_id,
                    repo_full_name=payload.repo_full_name,
                    comment_id=payload.comment_id,
                ),
                ACKNOWLEDGED,
            )
        except CommentGone:
            # Withdrawn before we got to it: nothing to acknowledge, and the job did not fail.
            logger.info(
                "repo_id=%d pr=%d: explain command deleted before the reaction",
                payload.repo_id,
                payload.pr_number,
            )
        return True
