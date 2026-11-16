"""What the analysis checkpoint of a run attempt needs to be rebuilt from the database (D-69).

``failures`` gains the fields of ``FailedJob`` that it did not keep, ``run_attempts`` gains the
checkpoint mark ``analyzed_at``, and ``diagnoses`` gains the call counts of the usage and an
exact ``confidence``. Exactly one diagnosis per failure can be published (partial unique index).

The tables are empty in every database that exists, so the new ``failures`` columns can be
``NOT NULL`` without a default.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = (
    # A failure can be rebuilt as a ``Failure`` (``FailedJob`` fields that 0001 did not keep).
    "ALTER TABLE failures ADD COLUMN conclusion text NOT NULL",
    "ALTER TABLE failures ADD COLUMN runner_os text",
    "ALTER TABLE failures ADD COLUMN started_at timestamptz NOT NULL",
    "ALTER TABLE failures ADD COLUMN completed_at timestamptz NOT NULL",
    "ALTER TABLE failures ADD COLUMN step_started_at timestamptz",
    "ALTER TABLE failures ADD COLUMN step_completed_at timestamptz",
    # The checkpoint mark: set in the same transaction that stores the analysis.
    "ALTER TABLE run_attempts ADD COLUMN analyzed_at timestamptz",
    # One published diagnosis per failure.
    "CREATE UNIQUE INDEX diagnoses_published_idx ON diagnoses (failure_id) WHERE published",
    # The usage of a job: how many calls answered and how many failed (they cost nothing).
    "ALTER TABLE diagnoses ADD COLUMN llm_calls int",
    "ALTER TABLE diagnoses ADD COLUMN llm_failed_calls int",
    # numeric(3,2) would round a confidence of 0.855, so a retry could publish another number
    # than the first run. A double keeps the value of ``Diagnosis.confidence`` exactly.
    "ALTER TABLE diagnoses ALTER COLUMN confidence TYPE double precision",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE diagnoses ALTER COLUMN confidence TYPE numeric(3,2) "
    "USING round(confidence::numeric, 2)",
    "ALTER TABLE diagnoses DROP COLUMN llm_failed_calls",
    "ALTER TABLE diagnoses DROP COLUMN llm_calls",
    "DROP INDEX diagnoses_published_idx",
    "ALTER TABLE run_attempts DROP COLUMN analyzed_at",
    "ALTER TABLE failures DROP COLUMN step_completed_at",
    "ALTER TABLE failures DROP COLUMN step_started_at",
    "ALTER TABLE failures DROP COLUMN completed_at",
    "ALTER TABLE failures DROP COLUMN started_at",
    "ALTER TABLE failures DROP COLUMN runner_os",
    "ALTER TABLE failures DROP COLUMN conclusion",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
