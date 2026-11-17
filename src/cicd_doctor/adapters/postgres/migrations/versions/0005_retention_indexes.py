"""Indexes for the daily purge (D-85), so no retention step scans a whole table.

Each one matches the predicate of one delete or update of the purge:

* ``webhook_deliveries (received_at)``: deliveries older than 30 days.
* ``jobs (updated_at)`` for finished jobs only (``done``, ``failed``, ``dead``): the partial index
  stays small because the live queue is not in it, and ``jobs_ready_idx`` keeps serving the leases.
* ``run_attempts (completed_at)``: attempts older than 90 days, which take their failures and
  diagnoses with them.
* ``failures (created_at)`` for ``open`` failures only: the ``stale`` mark of 30 days.
* ``check_publications`` and ``comment_publications (updated_at)``: publications older than 90 days.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = (
    "CREATE INDEX webhook_deliveries_received_idx ON webhook_deliveries (received_at)",
    """
    CREATE INDEX jobs_finished_idx ON jobs (updated_at)
        WHERE status IN ('done', 'failed', 'dead')
    """,
    "CREATE INDEX run_attempts_completed_idx ON run_attempts (completed_at)",
    "CREATE INDEX failures_open_created_idx ON failures (created_at) WHERE status = 'open'",
    "CREATE INDEX check_publications_updated_idx ON check_publications (updated_at)",
    "CREATE INDEX comment_publications_updated_idx ON comment_publications (updated_at)",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX comment_publications_updated_idx",
    "DROP INDEX check_publications_updated_idx",
    "DROP INDEX failures_open_created_idx",
    "DROP INDEX run_attempts_completed_idx",
    "DROP INDEX jobs_finished_idx",
    "DROP INDEX webhook_deliveries_received_idx",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
