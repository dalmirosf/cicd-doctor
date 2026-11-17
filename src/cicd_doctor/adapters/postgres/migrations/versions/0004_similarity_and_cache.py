"""The cache lookup of D-81 and what the exact similarity search of D-79 needs (nothing more).

* ``diagnoses_cache_idx``: the exact-signature cache reads only ``llm`` rows, one failure at a
  time. A partial index keeps that lookup away from the ``rules`` and ``cache`` rows, which are
  most of the table.
* No index for the similarity search. The exact query reads the rows of one repository through
  the prefix of ``failures_sig_idx`` (``repository_id``), checked with ``EXPLAIN`` in
  ``tests/integration/test_similarity_plan.py``; the same-run filter finds its rows through the
  prefix of the ``UNIQUE (run_id, run_attempt, job_id)`` index, so no ``failures_run_idx`` is
  needed. The HNSW index of 0001 stays untouched (D-79).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = (
    "CREATE INDEX diagnoses_cache_idx ON diagnoses (failure_id) WHERE source = 'llm'",
)

DOWNGRADE: tuple[str, ...] = ("DROP INDEX diagnoses_cache_idx",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
