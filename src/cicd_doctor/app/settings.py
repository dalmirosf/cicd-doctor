"""Application settings, read from the environment and ``.env`` (SPEC §4.5).

``.env.example`` is the contract: every variable in it is a field here, except the
``POSTGRES_*`` ones, which only Docker Compose reads. Empty values (``KEY=``) count as unset, so
the example file can list optional variables without giving them a value.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",  # POSTGRES_* and anything else in the environment
        frozen=True,
        hide_input_in_errors=True,  # a validation error must never echo DB_PASSWORD
    )

    app_env: Literal["dev", "prod"] = "dev"
    # Logs (D-117): ``json`` is one redacted object per line, for CloudWatch;
    # ``text`` for a terminal.
    log_format: Literal["json", "text"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # SecretStr: the URL carries the database password, so it must not show up in a repr.
    # Either DATABASE_URL (it always wins) or the DB_* parts (D-126), from which the URL is
    # built; ``database_url`` is the only value the rest of the code reads.
    database_url: SecretStr
    db_host: str | None = None
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str | None = None
    db_user: str | None = None
    db_password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _assemble_database_url(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("database_url"):
            return data
        host = data.get("db_host")
        if not host:
            raise ValueError(
                "no database configured: set DATABASE_URL, or DB_HOST (with DB_NAME, DB_USER, "
                "DB_PASSWORD and optionally DB_PORT) for database_url"
            )
        missing = [k.upper() for k in ("db_name", "db_user", "db_password") if not data.get(k)]
        if missing:
            raise ValueError(f"DB_HOST is set but {', '.join(missing)} is missing (database_url)")
        password = data["db_password"]
        if isinstance(password, SecretStr):
            password = password.get_secret_value()
        try:
            port = int(data.get("db_port") or 5432)
        except (TypeError, ValueError):
            raise ValueError("DB_PORT must be a number (database_url)") from None
        url = URL.create(
            "postgresql+psycopg",
            username=data["db_user"],
            password=password,
            host=host,
            port=port,
            database=data["db_name"],
        )
        return {**data, "database_url": url.render_as_string(hide_password=False)}

    # GitHub App (D-30). Required from P1c, when the GitHub adapter starts using them.
    github_app_id: int | None = None
    github_private_key: SecretStr | None = None  # PEM content or a path to a .pem file
    github_webhook_secret: SecretStr | None = None
    github_webhook_secret_previous: SecretStr | None = None  # only while rotating

    # LLM (D-07, D-16)
    llm_enabled: bool = True
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    llm_price_input_per_mtok: Decimal | None = None
    llm_price_output_per_mtok: Decimal | None = None
    llm_max_output_tokens: int = Field(default=800, gt=0)
    llm_timeout_s: float = Field(default=30, gt=0)
    llm_daily_calls_per_installation: int = Field(default=30, ge=0)
    llm_daily_budget_usd: Decimal = Field(default=Decimal("2.00"), ge=0)

    # Log processing (D-11)
    log_max_jobs: int = Field(default=3, ge=1)
    log_max_bytes: int = Field(default=20_971_520, gt=0)
    excerpt_max_tokens: int = Field(default=6000, gt=0)

    # Memory (P2)
    cache_ttl_days: int = Field(default=7, ge=0)
    similar_k: int = Field(default=3, ge=0)
    similar_min_score: float = Field(default=0.85, ge=0, le=1)
    similar_use_hnsw: bool = False  # D-79: the exact search is the default; pgvector >= 0.8

    # Commands (D-92, D-95): the most recent failed runs one `/cicd-doctor explain` diagnoses
    explain_max_runs: int = Field(default=3, ge=1)
    # Suggested changes (D-97): review comments with a `suggestion` block per run attempt; 0 = none
    explain_max_suggestions: int = Field(default=3, ge=0)

    # Repository configuration (D-90): how long one answer is kept in the process
    config_ttl_s: int = Field(default=300, ge=0)
    config_missing_ttl_s: int = Field(default=60, ge=0)

    # Embeddings (D-77, D-78)
    embeddings_enabled: bool = True
    embedding_cache_dir: Path = Path(".cache/fastembed")
    embedding_threads: int | None = Field(default=None, ge=1)  # unset: onnxruntime decides

    # Retention (FR-18, D-85)
    purge_enabled: bool = True
    purge_hour_utc: int = Field(default=3, ge=0, le=23)
    retention_deliveries_days: int = Field(default=30, ge=1)
    retention_failures_days: int = Field(default=90, ge=1)
    retention_jobs_days: int = Field(default=30, ge=1)
    stale_after_days: int = Field(default=30, ge=1)
    uninstall_grace_days: int = Field(default=7, ge=0)
    retention_commands_days: int = Field(default=30, ge=1)  # D-93

    # Commands (FR-19, D-93)
    command_window_s: int = Field(default=300, ge=1)  # one accepted explain per PR per window

    # Worker (SPEC §4.4, D-23)
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_s: int = Field(default=300, gt=0)
    job_max_attempts: int = Field(default=5, ge=1)

    # Observability (P5): tracing is disabled while these are unset
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
