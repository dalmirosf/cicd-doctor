# CICD Doctor

A GitHub App that explains *why* a GitHub Actions workflow run failed: it reads the logs of the
failed jobs, redacts secrets, looks for similar past failures and publishes a structured diagnosis
as a check run and a sticky PR comment.

> **Status: Phase 1b (offline pipeline), in progress.** The signed webhook, the Postgres job queue
> and the worker exist, and installations are tracked. The log pipeline (failed job selection,
> redacted excerpt, error signature) exists and is tested on synthetic logs; the rules, the LLM
> diagnosis and the GitHub output do not yet, so a failed run becomes a `diagnose_run` job that
> the worker completes with a placeholder handler.
> See [SPEC.md](SPEC.md) for the design and phases, and [DECISIONS.md](DECISIONS.md) for the
> reasoning behind each decision.

## Quickstart (Windows, PowerShell)

Requirements: [uv](https://docs.astral.sh/uv/), Docker Desktop and Git.

```powershell
uv sync                              # creates .venv and installs dependencies (downloads Python 3.13 if needed)
Copy-Item .env.example .env          # local configuration (git-ignored)
docker compose up -d db              # Postgres 17 + pgvector on 127.0.0.1:5433
uv run alembic upgrade head          # creates the schema (SPEC §6)
uv run cicd-doctor api               # http://127.0.0.1:8000
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health      # -> status: ok (liveness)
Invoke-RestMethod http://127.0.0.1:8000/ready       # -> status: ready (the database answers)
uv run cicd-doctor worker                            # job worker; Ctrl+C stops it cleanly
uv run pytest                                        # all tests (integration ones need the db)
```

### Receiving webhooks locally (dev GitHub App + smee.io)

Set `GITHUB_WEBHOOK_SECRET` in `.env` to the dev app's webhook secret, point the app's webhook URL
at your smee channel, then, with the API running:

```powershell
npx smee-client --url https://smee.io/<your-channel> --target http://127.0.0.1:8000/webhooks/github
```

A failed workflow run then shows up as a row in `webhook_deliveries` and a `diagnose_run` job.
`uv run python scripts/load_webhooks.py --cleanup` measures the acknowledgement latency with 100
signed deliveries (p99 must stay under 1 s).

If your `.env` is older than this README, check that `DATABASE_URL` uses `127.0.0.1` and not
`localhost`: on Windows `localhost` can try `::1` first and hang
([D-52](DECISIONS.md)).

Stop the database with `docker compose down` (add `-v` to also delete its data).

## Development checks

| What | Command |
|---|---|
| Tests (all) | `uv run pytest` |
| Fast tests (what pre-commit runs, no database) | `uv run pytest -m "unit or contract"` |
| Postgres tests (need `docker compose up -d db`) | `uv run pytest -m integration` |
| Migrate / roll back the schema | `uv run alembic upgrade head` / `uv run alembic downgrade base` |
| Lint / format check | `uv run ruff check .` / `uv run ruff format --check .` |
| Types | `uv run mypy` |
| Enable the commit gate (once per clone) | `uv run pre-commit install` |

The commit gate (ruff + fast tests) blocks a commit when it fails. CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, types and tests on every push and
pull request; the tests job has its own pgvector service, so integration tests run there too.
Locally they are skipped while the database is down. They use their own `cicd_doctor_test`
database and never touch your development data.

## Layout

```
src/cicd_doctor/
├── core/       # domain models, ports, errors, retry rules. Never imports adapters (tests/unit/test_architecture.py)
├── adapters/   # port implementations: github (webhooks), postgres (queue, installations, migrations), memory, clock
├── api/        # FastAPI app: GET /health, GET /ready, POST /webhooks/github
├── worker/     # job consumer and handlers
└── app/        # composition root, settings and CLI
scripts/        # load_webhooks.py (webhook latency check)
tests/          # unit/, contract/, integration/, fixtures/webhooks/ (synthetic payloads)
evals/          # eval harness (P1 skeleton, P4)
docker/         # compose.yaml (+ Dockerfile in P5)
```
