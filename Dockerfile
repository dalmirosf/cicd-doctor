# syntax=docker/dockerfile:1
# One image for the API, the worker and the one-off migration task (P5, D-119): the command
# decides. Defaults to the API. No secret and no .env is ever copied in (see .dockerignore).
#
#   docker build -t cicd-doctor .
#   docker run --rm cicd-doctor cicd-doctor worker      # the worker
#   docker run --rm cicd-doctor alembic upgrade head    # the migration task
#
# The two models the app downloads on first use are baked in at build time, so a container
# starts with no network: fastembed (EMBEDDING_CACHE_DIR) and the tiktoken o200k_base
# vocabulary (TIKTOKEN_CACHE_DIR).

ARG PYTHON_IMAGE=python:3.13-slim

# ---- build: dependencies, the project and the models (uv and pip stay in this stage) ----
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first: this layer changes only with pyproject.toml and uv.lock.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV EMBEDDING_CACHE_DIR=/opt/models/fastembed \
    TIKTOKEN_CACHE_DIR=/opt/models/tiktoken
RUN mkdir -p "$EMBEDDING_CACHE_DIR" "$TIKTOKEN_CACHE_DIR" \
    && /app/.venv/bin/python -c "import os, pathlib; \
from cicd_doctor.adapters.embeddings.fastembed_embedder import load_fastembed; \
load_fastembed(pathlib.Path(os.environ['EMBEDDING_CACHE_DIR']), None); \
import tiktoken; tiktoken.get_encoding('o200k_base')"

# ---- runtime: no uv, no build tools, non-root ----
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EMBEDDING_CACHE_DIR=/opt/models/fastembed \
    TIKTOKEN_CACHE_DIR=/opt/models/tiktoken \
    HF_HUB_OFFLINE=1
WORKDIR /app
# --chown: the models must be readable and writable by the app user (huggingface writes a
# trees cache next to the files and warns when it cannot).
COPY --from=build --chown=app:app /app /app
COPY --from=build --chown=app:app /opt/models /opt/models
USER 10001:10001
EXPOSE 8000

CMD ["cicd-doctor", "api", "--host", "0.0.0.0"]
