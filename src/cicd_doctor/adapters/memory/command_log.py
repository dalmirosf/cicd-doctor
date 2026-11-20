"""In-memory ``CommandLog`` (D-93): the same decisions as the Postgres one, for tests and evals."""

from datetime import datetime, timedelta

from cicd_doctor.core.models import PurgeCounts


class InMemoryCommandLog:
    """Implements ``CommandLog``. ``claim`` has no ``await`` between its read and its write, so on
    one event loop it is atomic without a lock (Postgres needs its advisory lock for that)."""

    def __init__(self) -> None:
        # (repo_id, comment_id) -> (pr_number, actor_login, created_at)
        self._rows: dict[tuple[int, int], tuple[int, str, datetime]] = {}

    async def claim(
        self,
        repo_id: int,
        pr_number: int,
        comment_id: int,
        actor_login: str,
        now: datetime,
        window: timedelta,
    ) -> bool:
        if (repo_id, comment_id) in self._rows:
            return True  # a retry of the same command
        for (row_repo, _), (row_pr, _, created_at) in self._rows.items():
            if row_repo == repo_id and row_pr == pr_number and created_at > now - window:
                return False
        self._rows[(repo_id, comment_id)] = (pr_number, actor_login, now)
        return True

    async def purge(self, before: datetime) -> PurgeCounts:
        old = [key for key, (_, _, created_at) in self._rows.items() if created_at < before]
        for key in old:
            del self._rows[key]
        return PurgeCounts(deleted={"command_invocations": len(old)})

    # --- Inspection (tests) -----------------------------------------------------------------

    def count(self, repo_id: int | None = None) -> int:
        return sum(1 for repo, _ in self._rows if repo_id is None or repo == repo_id)
