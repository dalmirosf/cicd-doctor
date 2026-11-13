"""Alembic environment (D-50).

Migrations are hand-written SQL (no SQLAlchemy metadata, no autogenerate) and run on a sync
psycopg engine: ``postgresql+psycopg://`` works for both the sync and the async engine, and the
sync one needs no event loop (D-52).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

# Tests set configure_logger=False so that running a migration does not replace pytest's logging.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return str(url)
    # Imported here so that tests passing a URL never need a .env file.
    from cicd_doctor.app.settings import Settings

    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit the SQL instead of running it (``alembic upgrade head --sql``)."""
    context.configure(url=_database_url(), target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # connect_timeout: fail instead of hanging when the host is unreachable (D-52).
    engine = create_engine(
        _database_url(), poolclass=pool.NullPool, connect_args={"connect_timeout": 10}
    )
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
