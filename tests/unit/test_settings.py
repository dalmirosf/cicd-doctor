from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from cicd_doctor.app.settings import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
COMPOSE_ONLY = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT"}
URL = "postgresql+psycopg://user:pw-not-real@localhost:5433/db"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's or CI's real environment out of these tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            keys.add(stripped.split("=", 1)[0])
    return keys


def test_defaults_match_the_spec_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    settings = Settings(_env_file=None)

    assert settings.app_env == "dev"
    assert settings.llm_enabled is True
    assert settings.llm_max_output_tokens == 800
    assert settings.llm_daily_budget_usd == Decimal("2.00")
    assert (settings.log_max_jobs, settings.log_max_bytes, settings.excerpt_max_tokens) == (
        3,
        20_971_520,
        6000,
    )
    assert (settings.worker_concurrency, settings.job_lease_s, settings.job_max_attempts) == (
        4,
        300,
        5,
    )
    assert settings.github_app_id is None
    assert settings.openai_api_key is None


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("GITHUB_APP_ID", "")
    monkeypatch.setenv("WORKER_CONCURRENCY", "")

    settings = Settings(_env_file=None)

    assert settings.github_app_id is None
    assert settings.worker_concurrency == 4


def test_secrets_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    settings = Settings(_env_file=None)

    assert "pw-not-real" not in repr(settings)
    assert "sk-test" not in repr(settings)
    assert settings.database_url.get_secret_value() == URL


@pytest.mark.parametrize(
    ("name", "value"),
    [("WORKER_CONCURRENCY", "0"), ("JOB_LEASE_S", "0"), ("JOB_MAX_ATTEMPTS", "0")],
)
def test_worker_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_example_lists_exactly_the_settings_and_compose_variables() -> None:
    fields = {name.upper() for name in Settings.model_fields}

    assert _example_keys() == fields | COMPOSE_ONLY


def test_env_example_is_a_valid_configuration() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")


def test_embedding_settings_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    settings = Settings(_env_file=None)

    assert settings.embeddings_enabled is True
    assert settings.embedding_cache_dir.as_posix() == ".cache/fastembed"
    assert settings.embedding_threads is None
    monkeypatch.setenv("EMBEDDING_THREADS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_a_command_diagnoses_at_most_three_runs_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    assert Settings(_env_file=None).explain_max_runs == 3
    monkeypatch.setenv("EXPLAIN_MAX_RUNS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_suggestions_default_to_three_per_attempt_and_zero_turns_them_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)

    assert Settings(_env_file=None).explain_max_suggestions == 3
    monkeypatch.setenv("EXPLAIN_MAX_SUGGESTIONS", "0")
    assert Settings(_env_file=None).explain_max_suggestions == 0
    monkeypatch.setenv("EXPLAIN_MAX_SUGGESTIONS", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
