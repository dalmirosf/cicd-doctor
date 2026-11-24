"""The Dockerfile, .dockerignore and compose stack (P5 (c), D-119), checked on the files only.

Building the image needs Docker; the live check is in the PR report and in the P5 live PR.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text("utf-8")
DOCKERIGNORE = [
    line.strip()
    for line in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "docker" / "compose.yaml").read_text("utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]


def _instructions(prefix: str) -> list[str]:
    return [line for line in DOCKERFILE.splitlines() if line.startswith(prefix)]


def test_dockerfile_is_multi_stage_and_runs_as_a_non_root_user() -> None:
    assert len(_instructions("FROM ")) >= 2
    users = _instructions("USER ")
    assert users, "the final stage must set USER"
    assert users[-1].split()[1] not in {"root", "0", "0:0"}
    assert DOCKERFILE.index("USER ") > DOCKERFILE.rindex("FROM ")  # in the last stage


def test_runtime_stage_has_no_uv_and_the_build_stage_keeps_it() -> None:
    runtime = DOCKERFILE[DOCKERFILE.rindex("FROM ") :]
    assert "astral-sh/uv" not in runtime
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert re.search(r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+", DOCKERFILE), "uv must be pinned"


def test_dockerfile_sets_no_secret_and_copies_no_env_file() -> None:
    secret_words = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE")
    for line in _instructions("ENV ") + _instructions("ARG "):
        for name in re.findall(r"([A-Z][A-Z0-9_]*)=", line):
            assert name in {"EMBEDDING_CACHE_DIR", "TIKTOKEN_CACHE_DIR"} or not any(
                word in name for word in secret_words
            ), f"{name} looks like a secret"
    assert ".env" not in re.sub(r"\.env\.example", "", " ".join(_instructions("COPY ")))
    assert ".pem" not in DOCKERFILE


def test_dockerfile_copies_alembic_and_bakes_both_model_caches() -> None:
    assert re.search(r"^COPY .*alembic\.ini", DOCKERFILE, re.MULTILINE)
    assert "load_fastembed" in DOCKERFILE
    assert "EMBEDDING_CACHE_DIR=/opt/models/fastembed" in DOCKERFILE
    assert "TIKTOKEN_CACHE_DIR=/opt/models/tiktoken" in DOCKERFILE
    assert "o200k_base" in DOCKERFILE
    # the copied models belong to the app user, or huggingface cannot write its trees cache
    assert re.search(r"COPY --from=build --chown=\S+ /opt/models /opt/models", DOCKERFILE)


def test_dockerfile_defaults_to_the_api_bound_to_every_interface() -> None:
    cmd = _instructions("CMD ")
    assert cmd
    assert "cicd-doctor" in cmd[-1]
    assert "api" in cmd[-1]
    assert "0.0.0.0" in cmd[-1]


def test_dockerignore_keeps_secrets_and_history_out_of_the_image() -> None:
    for pattern in (".git", ".env", ".env.*", "*.pem", "*.tfstate", "plans", ".venv", "infra"):
        assert pattern in DOCKERIGNORE, pattern
    assert "alembic.ini" not in DOCKERIGNORE
    assert "src" not in DOCKERIGNORE
    assert "README.md" not in DOCKERIGNORE  # pyproject's readme: the project build needs it


def test_compose_has_the_full_stack_and_keeps_db_alone_startable() -> None:
    assert {"db", "migrate", "api", "worker"} <= set(SERVICES)
    assert "profiles" not in SERVICES["db"]
    assert SERVICES["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert SERVICES["migrate"]["restart"] == "no"
    assert SERVICES["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    for name in ("api", "worker"):
        assert SERVICES[name]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )


def test_compose_api_has_a_python_healthcheck_on_ready_and_a_loopback_port() -> None:
    api = SERVICES["api"]
    test = api["healthcheck"]["test"]
    assert test[:2] == ["CMD", "python"]
    assert "/ready" in " ".join(test)
    assert "curl" not in " ".join(test)
    assert all(port.startswith("127.0.0.1:") for port in api["ports"])
    assert "0.0.0.0" in api["command"]


def test_compose_app_services_use_the_db_service_and_a_mounted_key() -> None:
    for name in ("migrate", "api", "worker"):
        env = SERVICES[name]["environment"]
        assert "@db:5432/" in env["DATABASE_URL"]
        assert env["GITHUB_PRIVATE_KEY"] == "/run/secrets/github_app_key"
        assert SERVICES[name]["secrets"] == ["github_app_key"]
        assert SERVICES[name]["build"]["dockerfile"] == "Dockerfile"
    assert "github_app_key" in COMPOSE["secrets"]
    text = (ROOT / "docker" / "compose.yaml").read_text("utf-8")
    assert "BEGIN" not in text
