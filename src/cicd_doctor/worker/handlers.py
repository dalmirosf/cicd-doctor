"""Job handlers: turn a leased ``Job`` into a use case call."""

from pydantic import ValidationError

from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import InstallationEvent, Job
from cicd_doctor.core.ports import InstallationStore
from cicd_doctor.core.usecases.track_installation import track_installation
from cicd_doctor.worker.runner import Handler


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
