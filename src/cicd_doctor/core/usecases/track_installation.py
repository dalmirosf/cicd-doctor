"""Use case: apply an installation webhook to the stored installations and repositories (FR-03).

Every step is an upsert, so running the same event twice (a retried job) gives the same state.
Two events of one installation handled out of order can leave a stale state until the next
event; installation events are rare, and P1c also refreshes the repository from each
``workflow_run`` (D-54).
"""

from cicd_doctor.core.models import Installation, InstallationAction, InstallationEvent
from cicd_doctor.core.ports import InstallationStore


async def track_installation(event: InstallationEvent, store: InstallationStore) -> None:
    current = await store.installation(event.installation.id)
    installation = _apply(event, current)
    await store.save_installation(installation)
    if event.action is not InstallationAction.DELETED and event.repositories:
        await store.save_repositories(
            [repo.model_copy(update={"removed_at": None}) for repo in event.repositories]
        )
    if event.removed_repository_ids:
        await store.remove_repositories(event.removed_repository_ids, event.occurred_at)


def _apply(event: InstallationEvent, current: Installation | None) -> Installation:
    account = {
        "account_login": event.installation.account_login,
        "account_type": event.installation.account_type,
    }
    base = event.installation if current is None else current.model_copy(update=account)
    match event.action:
        case InstallationAction.CREATED:
            return base.model_copy(update={"suspended_at": None, "deleted_at": None})
        case InstallationAction.DELETED:
            return base.model_copy(update={"deleted_at": event.occurred_at})
        case InstallationAction.SUSPENDED:
            return base.model_copy(update={"suspended_at": event.occurred_at})
        case InstallationAction.UNSUSPENDED:
            return base.model_copy(update={"suspended_at": None})
        case InstallationAction.REPOSITORIES_ADDED | InstallationAction.REPOSITORIES_REMOVED:
            return base
