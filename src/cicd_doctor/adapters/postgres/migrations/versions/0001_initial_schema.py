"""Initial schema (SPEC §6).

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL only (D-50): one op.execute per statement, kept in sync with SPEC §6.
UPGRADE: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE TYPE job_status AS ENUM ('queued', 'running', 'done', 'failed', 'dead')",
    """
    CREATE TYPE failure_category AS ENUM (
        'broken_test', 'dependency', 'network', 'config_or_secrets',
        'timeout', 'infra_or_deploy', 'flaky', 'unknown'
    )
    """,
    "CREATE TYPE diagnosis_source AS ENUM ('rules', 'llm', 'cache')",
    """
    CREATE TYPE failure_status AS ENUM ('open', 'resolved_flaky', 'resolved_by_commit', 'stale')
    """,
    # --- GitHub entities ------------------------------------------------------------------
    """
    CREATE TABLE installations (
        id             bigint PRIMARY KEY,
        account_login  text   NOT NULL,
        account_type   text   NOT NULL,
        suspended_at   timestamptz,
        deleted_at     timestamptz,
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE repositories (
        id              bigint PRIMARY KEY,
        installation_id bigint NOT NULL REFERENCES installations(id),
        full_name       text    NOT NULL,
        private         boolean NOT NULL,
        default_branch  text    NOT NULL,
        removed_at      timestamptz,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    # --- Ingestion + queue ----------------------------------------------------------------
    """
    CREATE TABLE webhook_deliveries (
        delivery_id     uuid PRIMARY KEY,
        event           text NOT NULL,
        action          text,
        installation_id bigint,
        received_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE jobs (
        id           bigserial PRIMARY KEY,
        kind         text   NOT NULL,
        payload      jsonb  NOT NULL,
        dedupe_key   text   UNIQUE,
        delivery_id  uuid   REFERENCES webhook_deliveries(delivery_id) ON DELETE SET NULL,
        status       job_status NOT NULL DEFAULT 'queued',
        attempts     int    NOT NULL DEFAULT 0,
        max_attempts int    NOT NULL DEFAULT 5,
        run_after    timestamptz NOT NULL DEFAULT now(),
        locked_until timestamptz,
        locked_by    text,
        last_error   text,
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX jobs_ready_idx ON jobs (run_after) WHERE status IN ('queued', 'running')",
    # --- Runs and failures ----------------------------------------------------------------
    """
    CREATE TABLE workflow_runs (
        id                  bigint PRIMARY KEY,
        repository_id       bigint NOT NULL REFERENCES repositories(id),
        workflow_id         bigint NOT NULL,
        workflow_name       text   NOT NULL,
        head_sha            char(40) NOT NULL,
        head_branch         text,
        head_repo_full_name text   NOT NULL,
        event               text   NOT NULL,
        is_fork             boolean NOT NULL,
        pr_number           int,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX workflow_runs_branch_idx
        ON workflow_runs (repository_id, workflow_id, head_repo_full_name, head_branch)
    """,
    """
    CREATE TABLE run_attempts (
        run_id       bigint NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
        attempt      int    NOT NULL,
        conclusion   text   NOT NULL,
        completed_at timestamptz NOT NULL,
        PRIMARY KEY (run_id, attempt)
    )
    """,
    """
    CREATE TABLE failures (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        repository_id   bigint NOT NULL REFERENCES repositories(id),
        run_id          bigint NOT NULL,
        run_attempt     int    NOT NULL,
        job_id          bigint NOT NULL,
        job_name        text   NOT NULL,
        step_name       text,
        step_number     int,
        signature_hash  char(64) NOT NULL,
        signature_text  text   NOT NULL,
        excerpt         jsonb  NOT NULL,
        excerpt_tokens  int    NOT NULL,
        truncated       boolean NOT NULL DEFAULT false,
        embedding       vector(384),
        status          failure_status NOT NULL DEFAULT 'open',
        fix_commit_sha  char(40),
        resolved_at     timestamptz,
        created_at      timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (run_id, run_attempt)
            REFERENCES run_attempts(run_id, attempt) ON DELETE CASCADE,
        UNIQUE (run_id, run_attempt, job_id)
    )
    """,
    """
    CREATE INDEX failures_sig_idx ON failures (repository_id, signature_hash, created_at DESC)
    """,
    "CREATE INDEX failures_open_idx ON failures (repository_id, status) WHERE status = 'open'",
    "CREATE INDEX failures_emb_idx ON failures USING hnsw (embedding vector_cosine_ops)",
    """
    CREATE TABLE diagnoses (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        failure_id        uuid NOT NULL REFERENCES failures(id) ON DELETE CASCADE,
        source            diagnosis_source NOT NULL,
        published         boolean NOT NULL DEFAULT false,
        category          failure_category NOT NULL,
        probable_cause    text  NOT NULL,
        evidence          jsonb NOT NULL,
        evidence_verified boolean NOT NULL DEFAULT true,
        suggested_fix     text  NOT NULL,
        suggestion        jsonb,
        confidence        numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        rule_ids          text[] NOT NULL DEFAULT '{}',
        cached_from       uuid REFERENCES diagnoses(id) ON DELETE SET NULL,
        model             text,
        prompt_version    text,
        input_tokens      int,
        output_tokens     int,
        cost_usd          numeric(10,6),
        latency_ms        int,
        fallback_reason   text,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX diagnoses_failure_idx ON diagnoses (failure_id)",
    # --- Publication bookkeeping ----------------------------------------------------------
    """
    CREATE TABLE check_publications (
        repository_id bigint NOT NULL REFERENCES repositories(id),
        run_id        bigint NOT NULL,
        check_run_id  bigint NOT NULL,
        last_attempt  int    NOT NULL,
        updated_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (repository_id, run_id)
    )
    """,
    """
    CREATE TABLE comment_publications (
        repository_id bigint NOT NULL REFERENCES repositories(id),
        pr_number     int    NOT NULL,
        workflow_id   bigint NOT NULL,
        comment_id    bigint NOT NULL,
        last_run_id   bigint NOT NULL,
        last_attempt  int    NOT NULL,
        updated_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (repository_id, pr_number, workflow_id)
    )
    """,
    # --- Cost control ---------------------------------------------------------------------
    """
    CREATE TABLE llm_usage (
        day             date   NOT NULL,
        installation_id bigint NOT NULL,
        calls           int    NOT NULL DEFAULT 0,
        reserved_usd    numeric(10,6) NOT NULL DEFAULT 0,
        spent_usd       numeric(10,6) NOT NULL DEFAULT 0,
        PRIMARY KEY (day, installation_id)
    )
    """,
    # --- P3 -------------------------------------------------------------------------------
    """
    CREATE TABLE command_invocations (
        repository_id bigint NOT NULL,
        pr_number     int    NOT NULL,
        comment_id    bigint NOT NULL,
        actor_login   text   NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (repository_id, comment_id)
    )
    """,
    """
    CREATE INDEX command_invocations_pr_idx
        ON command_invocations (repository_id, pr_number, created_at DESC)
    """,
)

# Reverse dependency order. The vector extension stays: the Compose init script also creates it,
# and other databases on the same server may use it.
DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE command_invocations",
    "DROP TABLE llm_usage",
    "DROP TABLE comment_publications",
    "DROP TABLE check_publications",
    "DROP TABLE diagnoses",
    "DROP TABLE failures",
    "DROP TABLE run_attempts",
    "DROP TABLE workflow_runs",
    "DROP TABLE jobs",
    "DROP TABLE webhook_deliveries",
    "DROP TABLE repositories",
    "DROP TABLE installations",
    "DROP TYPE failure_status",
    "DROP TYPE diagnosis_source",
    "DROP TYPE failure_category",
    "DROP TYPE job_status",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
