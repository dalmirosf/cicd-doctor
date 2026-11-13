"""Postgres ``InstallationStore`` (FR-03, D-54): upserts into ``installations`` and
``repositories``."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Self

from sqlalchemy import RowMapping, TextClause, text
from sqlalchemy.ext.asyncio import AsyncEngine

from cicd_doctor.adapters.postgres.engine import make_engine, transient_db_errors
from cicd_doctor.core.models import Installation, Repository

_SAVE_INSTALLATION = text(
    """
    INSERT INTO installations (id, account_login, account_type, suspended_at, deleted_at)
    VALUES (:id, :account_login, :account_type, :suspended_at, :deleted_at)
    ON CONFLICT (id) DO UPDATE
    SET account_login = EXCLUDED.account_login, account_type = EXCLUDED.account_type,
        suspended_at = EXCLUDED.suspended_at, deleted_at = EXCLUDED.deleted_at
    """
)
_SAVE_REPOSITORY = text(
    """
    INSERT INTO repositories (id, installation_id, full_name, private, default_branch, removed_at)
    VALUES (:id, :installation_id, :full_name, :private, :default_branch, :removed_at)
    ON CONFLICT (id) DO UPDATE
    SET installation_id = EXCLUDED.installation_id, full_name = EXCLUDED.full_name,
        private = EXCLUDED.private,
        default_branch = COALESCE(EXCLUDED.default_branch, repositories.default_branch),
        removed_at = EXCLUDED.removed_at
    """
)
_REMOVE_REPOSITORIES = text(
    "UPDATE repositories SET removed_at = :removed_at WHERE id = ANY(CAST(:ids AS bigint[]))"
)
_GET_INSTALLATION = text(
    "SELECT id, account_login, account_type, suspended_at, deleted_at FROM installations "
    "WHERE id = :id"
)
_GET_REPOSITORY = text(
    "SELECT id, installation_id, full_name, private, default_branch, removed_at FROM repositories "
    "WHERE id = :id"
)


class PostgresInstallationStore:
    """Implements ``InstallationStore``. Create it with ``PostgresInstallationStore.connect``."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str, *, pool_size: int = 2) -> AsyncIterator[Self]:
        engine = make_engine(database_url, pool_size=pool_size)
        try:
            yield cls(engine)
        finally:
            await engine.dispose()

    async def save_installation(self, installation: Installation) -> None:
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(_SAVE_INSTALLATION, installation.model_dump())

    async def save_repositories(self, repositories: Sequence[Repository]) -> None:
        if not repositories:
            return
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(_SAVE_REPOSITORY, [repo.model_dump() for repo in repositories])

    async def remove_repositories(self, repo_ids: Sequence[int], removed_at: datetime) -> None:
        if not repo_ids:
            return
        with transient_db_errors():
            async with self._engine.begin() as conn:
                await conn.execute(
                    _REMOVE_REPOSITORIES, {"ids": list(repo_ids), "removed_at": removed_at}
                )

    async def installation(self, installation_id: int) -> Installation | None:
        row = await self._one(_GET_INSTALLATION, installation_id)
        return None if row is None else Installation.model_validate(dict(row))

    async def repository(self, repo_id: int) -> Repository | None:
        row = await self._one(_GET_REPOSITORY, repo_id)
        return None if row is None else Repository.model_validate(dict(row))

    async def _one(self, statement: TextClause, key: int) -> RowMapping | None:
        with transient_db_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(statement, {"id": key})).first()
        return None if row is None else row._mapping
