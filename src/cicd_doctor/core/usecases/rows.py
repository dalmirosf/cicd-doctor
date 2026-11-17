"""The installation and repository rows a run needs before anything is stored (D-75)."""

from cicd_doctor.core.errors import PermanentError
from cicd_doctor.core.models import Installation, Repository, RunRef
from cicd_doctor.core.ports import InstallationStore


async def ensure_rows(
    installations: InstallationStore,
    run: RunRef,
    *,
    account_login: str | None,
    account_type: str | None,
    private: bool,
    default_branch: str,
) -> None:
    """Make the installation and repository rows exist, before anything else.

    A removed repository or a deleted installation still has a row. A job queued before P1c
    carries no account: if nothing tracks its installation either, retrying cannot help.
    """
    if account_login and account_type:
        await installations.ensure_installation(
            Installation(
                id=run.installation_id, account_login=account_login, account_type=account_type
            )
        )
    elif await installations.installation(run.installation_id) is None:
        raise PermanentError(
            f"installation {run.installation_id} is unknown and the job has no account"
        )
    await installations.save_repositories(
        [
            Repository(
                id=run.repo_id,
                installation_id=run.installation_id,
                full_name=run.repo_full_name,
                private=private,
                default_branch=default_branch,
            )
        ]
    )
