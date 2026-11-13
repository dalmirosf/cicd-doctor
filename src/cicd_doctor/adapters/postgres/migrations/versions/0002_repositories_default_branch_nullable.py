"""repositories.default_branch becomes nullable (D-54).

``installation`` and ``installation_repositories`` webhooks list repositories without their
default branch; it is filled in from the first ``workflow_run`` of the repository (P1c).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = ("ALTER TABLE repositories ALTER COLUMN default_branch DROP NOT NULL",)
# Rows without a default branch would block SET NOT NULL, so they get the empty string first.
DOWNGRADE: tuple[str, ...] = (
    "UPDATE repositories SET default_branch = '' WHERE default_branch IS NULL",
    "ALTER TABLE repositories ALTER COLUMN default_branch SET NOT NULL",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
