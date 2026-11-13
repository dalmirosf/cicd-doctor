"""Application settings, read from the environment and ``.env`` (SPEC §4.5).

``.env.example`` is the contract: every variable in it is a field here, except the
``POSTGRES_*`` ones, which only Docker Compose reads. Empty values (``KEY=``) count as unset, so
the example file can list optional variables without giving them a value.
"""

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",  # POSTGRES_* and anything else in the environment
        frozen=True,
    )

    app_env: Literal["dev", "prod"] = "dev"
    # SecretStr: the URL carries the database password, so it must not show up in a repr.
    database_url: SecretStr

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

    # Worker (SPEC §4.4, D-23)
    worker_concurrency: int = Field(default=4, ge=1)
    job_lease_s: int = Field(default=300, gt=0)
    job_max_attempts: int = Field(default=5, ge=1)

    # Observability (P5): tracing is disabled while these are unset
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
