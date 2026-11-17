"""Job handlers: turn a leased ``Job`` into a use case call."""

import asyncio
import logging
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from cicd_doctor.core.errors import InstallationUnavailable, LogsUnavailable, PermanentError
from cicd_doctor.core.models import DiagnoseRunPayload, InstallationEvent, Job, RunRef
from cicd_doctor.core.ports import Clock, FailureStore, InstallationStore, JobQueue
from cicd_doctor.core.usecases.purge import PurgeConfig, run_purge
from cicd_doctor.core.usecases.track_installation import track_installation
from cicd_doctor.worker.runner import Handler

logger = logging.getLogger(__name__)

# "Could not diagnose" is published after the work failed, on top of whatever GitHub is doing
# badly, so it gets its own bound and never holds the job (and its lease) for long.
GIVE_UP_TIMEOUT_S = 30.0
PERMISSION_STATUS = 403


def sync_installation_handler(store: InstallationStore) -> Handler:
    """``sync_installation`` jobs (FR-03, D-54)."""

    async def handle(job: Job) -> None:
        try:
            event = InstallationEvent.model_validate(job.payload)
        except ValidationError as exc:
            # Retrying cannot fix a malformed payload.
            raise PermanentError(
                f"invalid sync_installation payload ({exc.error_count()} errors)"
            ) from exc
        await track_installation(event, store)

    return handle


def purge_handler(
    queue: JobQueue,
    store: FailureStore,
    installations: InstallationStore,
    clock: Clock,
    config: PurgeConfig,
) -> Handler:
    """``purge`` jobs (D-85). The payload is empty; the cuts come from the clock and the config.
    A failure is retried by the worker, and every step is idempotent."""

    async def handle(job: Job) -> None:
        await run_purge(queue, store, installations, clock, config)

    return handle


class DiagnoseUseCase(Protocol):
    """What the handler needs of ``DiagnoseFailedRun``."""

    async def run(self, payload: DiagnoseRunPayload) -> None: ...

    async def publish_could_not_diagnose(
        self,
        run: RunRef,
        reason: str,
        completed_at: datetime | None = None,
        *,
        only_if_started: bool = False,
    ) -> bool: ...


def diagnose_run_handler(
    use_case: DiagnoseUseCase, *, give_up_timeout_s: float = GIVE_UP_TIMEOUT_S
) -> Handler:
    """``diagnose_run`` jobs (D-74).

    The use case does the work. When it fails for good (a ``PermanentError``) or on the last
    attempt, the check run would stay ``in_progress`` for ever, so the handler completes it as
    "Could not diagnose" with the reason, as a best effort, and then re-raises the original
    error so the worker records ``failed`` or ``dead`` (D-49).

    A ``cancelled`` run has no check run until the use case decides it was a job timeout
    (D-73), so for it nothing is published unless the check run of this attempt already
    started: a manual cancellation stays silent even when reading its annotations failed.
    """

    async def handle(job: Job) -> None:
        try:
            payload = DiagnoseRunPayload.model_validate(job.payload)
        except ValidationError as exc:
            # Retrying cannot fix a malformed payload, and there is no run to report on.
            raise PermanentError(
                f"invalid diagnose_run payload ({exc.error_count()} errors)"
            ) from exc
        try:
            await use_case.run(payload)
        except InstallationUnavailable:
            raise  # no token: there is nowhere to publish
        except Exception as exc:
            if isinstance(exc, PermanentError) or job.attempts >= job.max_attempts:
                await _give_up(use_case, payload, _reason(exc), give_up_timeout_s)
            raise

    return handle


def _reason(exc: Exception) -> str:
    """The ``COULD_NOT_DIAGNOSE`` key for an error the job gives up on."""
    if isinstance(exc, LogsUnavailable):
        return "logs_expired"
    if getattr(exc, "status_code", None) == PERMISSION_STATUS and isinstance(exc, PermanentError):
        return "permission"
    return "internal_error"


async def _give_up(
    use_case: DiagnoseUseCase, payload: DiagnoseRunPayload, reason: str, timeout_s: float
) -> None:
    """Best effort: nothing that goes wrong here may hide the error that got us here."""
    try:
        async with asyncio.timeout(timeout_s):
            await use_case.publish_could_not_diagnose(
                payload.run,
                reason,
                payload.completed_at,
                only_if_started=payload.conclusion == "cancelled",
            )
    except Exception:  # not CancelledError: a shutdown must still stop the job
        logger.warning(
            "run_id=%d attempt=%d: could not publish 'Could not diagnose' (%s)",
            payload.run.run_id,
            payload.run.run_attempt,
            reason,
            exc_info=True,
        )
