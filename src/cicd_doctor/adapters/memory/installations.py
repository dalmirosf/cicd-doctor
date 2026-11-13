"""In-memory ``InstallationStore``: reference behaviour for the contract tests."""

from collections.abc import Sequence
from datetime import datetime

from cicd_doctor.core.models import Installation, Repository


class InMemoryInstallationStore:
    """Implements ``InstallationStore``."""

    def __init__(self) -> None:
        self._installations: dict[int, Installation] = {}
        self._repositories: dict[int, Repository] = {}

    async def save_installation(self, installation: Installation) -> None:
        self._installations[installation.id] = installation

    async def save_repositories(self, repositories: Sequence[Repository]) -> None:
        for repo in repositories:
            if repo.installation_id not in self._installations:
                raise KeyError(f"unknown installation {repo.installation_id}")  # like the FK
            stored = self._repositories.get(repo.id)
            if repo.default_branch is None and stored is not None:
                repo = repo.model_copy(update={"default_branch": stored.default_branch})
            self._repositories[repo.id] = repo

    async def remove_repositories(self, repo_ids: Sequence[int], removed_at: datetime) -> None:
        for repo_id in repo_ids:
            stored = self._repositories.get(repo_id)
            if stored is not None:
                self._repositories[repo_id] = stored.model_copy(update={"removed_at": removed_at})

    async def installation(self, installation_id: int) -> Installation | None:
        return self._installations.get(installation_id)

    async def repository(self, repo_id: int) -> Repository | None:
        return self._repositories.get(repo_id)
