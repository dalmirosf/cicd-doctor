# CICD Doctor

A GitHub App that explains *why* a GitHub Actions workflow run failed: it reads the logs of the
failed jobs, redacts secrets, looks for similar past failures and publishes a structured diagnosis
as a check run and a sticky PR comment.

> **Status: Phase 0 (skeleton).** There is no business logic yet. See [SPEC.md](SPEC.md) for the
> design and phases, and [DECISIONS.md](DECISIONS.md) for the reasoning behind each decision.

## Quickstart (Windows, PowerShell)

Requirements: [uv](https://docs.astral.sh/uv/), Docker Desktop and Git.

```powershell
uv sync                              # creates .venv and installs dependencies (downloads Python 3.13 if needed)
Copy-Item .env.example .env          # local configuration (git-ignored)
docker compose up -d db              # Postgres 17 + pgvector on 127.0.0.1:5433
uv run cicd-doctor api               # http://127.0.0.1:8000
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health      # -> status: ok
uv run pytest                                        # all tests
```

Stop the database with `docker compose down` (add `-v` to also delete its data).

## Development checks

| What | Command |
|---|---|
| Tests (all) | `uv run pytest` |
| Fast tests (what pre-commit runs) | `uv run pytest -m "unit or contract"` |
| Lint / format check | `uv run ruff check .` / `uv run ruff format --check .` |
| Types | `uv run mypy` |
| Enable the commit gate (once per clone) | `uv run pre-commit install` |

The commit gate (ruff + fast tests) blocks a commit when it fails. CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, types and tests on every push and
pull request.

## Layout

```
src/cicd_doctor/
├── core/       # domain models, ports, errors. Never imports adapters (tests/unit/test_architecture.py)
├── adapters/   # port implementations: memory (store, FakeEmbedder), clock; github/llm/postgres later
├── api/        # FastAPI app (GET /health today; webhook in P1)
├── worker/     # job consumer (P1)
└── app/        # composition root and CLI
tests/          # unit/, contract/, adapters/, integration/, e2e/, fixtures/
evals/          # eval harness (P1 skeleton, P4)
docker/         # compose.yaml (+ Dockerfile in P5)
```
