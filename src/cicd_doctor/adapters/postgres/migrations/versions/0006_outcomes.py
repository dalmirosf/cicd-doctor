"""Which success resolved a failure (D-83, D-84).

* ``failures.resolved_by_run_id`` and ``resolved_by_attempt``: the ``(run_id, attempt)`` of the
  successful attempt that resolved the failure; both NULL while it is open or ``stale``. A retry of
  the outcome job reads them back to rewrite the comment when the first try failed after
  ``resolve``. No foreign key: the success may be purged before the failure.
* ``failures_resolved_by_idx``: that lookup, partial so the open failures are not in it.
* No index for ``open_failures`` by branch: measured at 10 ms with 90 000 runs, see
  ``tests/integration/test_outcomes_plan.py``, so the write cost of one is not paid.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = (
    "ALTER TABLE failures ADD COLUMN resolved_by_run_id bigint, ADD COLUMN resolved_by_attempt int",
    """
    CREATE INDEX failures_resolved_by_idx ON failures (resolved_by_run_id, resolved_by_attempt)
        WHERE resolved_by_run_id IS NOT NULL
    """,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX failures_resolved_by_idx",
    "ALTER TABLE failures DROP COLUMN resolved_by_attempt, DROP COLUMN resolved_by_run_id",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
