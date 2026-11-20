"""A programmable ``RepoConfigSource`` for tests: it answers what the test set and records calls."""

from cicd_doctor.core.models import LoadedConfig, RepoRef


class FakeRepoConfig:
    """Answers the same ``LoadedConfig`` for every repository unless one was set for its id.

    ``fail_with`` makes the next ``times`` calls raise (a ``TransientError``, say).
    """

    def __init__(self, default: LoadedConfig | None = None) -> None:
        self._default = default or LoadedConfig()
        self._by_repo: dict[int, LoadedConfig] = {}
        self._failure: Exception | None = None
        self._failures_left = 0
        self.calls: list[RepoRef] = []

    def set(self, loaded: LoadedConfig, repo_id: int | None = None) -> None:
        if repo_id is None:
            self._default = loaded
        else:
            self._by_repo[repo_id] = loaded

    def fail_with(self, error: Exception, times: int = 1) -> None:
        self._failure = error
        self._failures_left = times

    async def load(self, repo: RepoRef) -> LoadedConfig:
        self.calls.append(repo)
        if self._failure is not None and self._failures_left > 0:
            self._failures_left -= 1
            raise self._failure
        return self._by_repo.get(repo.repo_id, self._default)
