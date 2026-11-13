# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CICD Doctor is a GitHub App that explains why a GitHub Actions run failed: failed-job logs → redacted excerpt → error signature → rules + LLM diagnosis → neutral check run and a sticky PR comment. **`SPEC.md` is the design and `DECISIONS.md` is the decision log** (entries `D-NN`, cited from the spec, the code and the tests). Read the relevant sections before designing anything.

- `SPEC.md` describes the whole target across phases P0–P6, but **only Phase 0 exists**: core models and ports, in-memory adapters, `GET /health` and the tooling. The webhook, queue, worker command, rules, LLM, GitHub/Postgres adapters and Alembic are not implemented. The empty packages (`adapters/{github,llm,embeddings,postgres}`, `core/{logs,rules,diagnosis,usecases}`, `worker/`) are placeholders. The "Implemented as of P0" paragraph under the §4.2 tree and the phase tags in the spec say what exists; do not assume a module or command does.
- Keep the docs in sync. When behaviour departs from the spec, append a new `D-NN` to `DECISIONS.md` (append-only: mark replaced decisions "Superseded by D-NN", or add an "Amended by" line), and update the affected `SPEC.md` sections. Tick §13 acceptance criteria only for things actually verified.
- Everything in the repo is English (D-36): code, comments, docs, commit messages. No `Co-Authored-By` or other attribution lines in commits or PRs (the author's settings turn attribution off).

## Commands (Windows, PowerShell)

```powershell
uv sync                                    # .venv + dependencies (Python 3.13)
uv run pytest                              # whole suite
uv run pytest -m "unit or contract"        # fast suite: what pre-commit and the Claude hook run
uv run pytest tests/unit/test_models.py::test_branch_key_includes_head_repo_so_forks_do_not_collide
uv run pytest tests/contract -k similar    # by keyword; parametrized ids look like test_x[memory]
uv run ruff check .                        # lint
uv run ruff format .                       # the Claude hook and CI only *check* formatting; this fixes it
uv run mypy                                # strict, covers src and tests; not part of pre-commit
uv run pre-commit install                  # once per clone: commit gate; it rewrites files, then fails the commit so you re-stage
Copy-Item .env.example .env                # only POSTGRES_* is read today
docker compose up -d db                    # Postgres 17 + pgvector on 127.0.0.1:5433, run from the repo root
docker compose down                        # keeps the data volume (add -v to delete it)
uv run cicd-doctor api                     # http://127.0.0.1:8000, GET /health
```

## Architecture

- **Hexagonal (D-02).** `core/` holds the models (`models.py`), the ports (`ports.py`, `Protocol`s) and the errors, and later the pure domain logic and use cases. Anything external goes through a port. `adapters/` implements the ports, `app/` is the composition root (settings, wiring, CLI), and `api/` and `worker/` are inbound adapters that sit beside `adapters/` (D-39).
- **Enforced import rule.** `core/` must not import `adapters`, `app`, `api`, `worker`, or `fastapi`, `starlette`, `uvicorn`, `httpx`, `sqlalchemy`, `psycopg`, `openai`, `fastembed` (Pydantic is allowed, D-03). `tests/unit/test_architecture.py` checks it statically with `ast`; there is no import-linter (D-43). A new forbidden library means editing `FORBIDDEN_EXTERNAL` in that test.
- **Models are frozen Pydantic v2.** Change them with `model_copy(update=...)`. Types that the ports need but SPEC §5 does not define are "P0 placeholders" (SPEC §5.2) and are expected to be refined in P1.
- **Port contract tests.** `tests/contract/test_failure_store.py` runs one suite against every `FailureStore` through its `_FACTORIES` dict. A new implementation (Postgres, P1) is added there and must pass the same tests unchanged. `adapters/memory/` and `adapters/clock.py` are the reference behaviour and the test doubles; `FakeEmbedder` is a deterministic 384-d feature-hashing stand-in for the future fastembed embedder.
- **Invariants worth knowing before touching the store or the models:** every lookup is scoped by `repo_id`, with no cross-repository cache or similarity (D-10); publications are idempotent and the newer `(run_id, attempt)` pair wins in `save_publication` (D-06); raw logs are never stored or logged, so `RawLog.text` has `repr=False` (D-12).

## Testing conventions

- Markers `unit`, `contract`, `integration` and `e2e` are applied automatically from the top-level directory under `tests/` (`tests/conftest.py`), and `--strict-markers` is on. Do not add per-file markers.
- `tests/` is a package and helpers are imported as `from tests.factories import make_failure`. Keep an `__init__.py` in every test directory that holds tests (`adapters/`, `integration/`, `e2e/` have only `.gitkeep` so far).
- `asyncio_mode = "auto"`: async tests need no decorator. mypy strict also checks the tests, so annotate them.

## Gates and gotchas

- A Claude Code `PostToolUse` hook (`.claude/settings.json` → `.claude/hooks/post_edit.py`) runs `ruff check`, `ruff format --check` and the fast suite after each edit to a `.py`, `.pyi` or `pyproject.toml` file. A failure comes back as a blocking error: fix it before continuing. It only checks and never rewrites files.
- CI (`.github/workflows/ci.yml`) has three jobs (lint, types, tests) and uses `uv sync --locked`, so commit `uv.lock` together with any `pyproject.toml` change. Actions are pinned by commit SHA with the version in a comment; keep that style.
- Ruff excludes `*.md` on purpose: ruff 0.17 reformats code blocks inside Markdown, which would rewrite the hand-written `SPEC.md` and `DECISIONS.md`. Do not remove `extend-exclude`.
- Files are LF (`.gitattributes`). Windows PowerShell 5.1 `Set-Content -Encoding utf8` and `Out-File` write a BOM, which makes Python reject the file. Write files with the Write tool, or with `[IO.File]::WriteAllText(path, text, (New-Object Text.UTF8Encoding $false))`.
- Postgres is published on host port 5433 because a native Postgres usually owns 5432 on this machine (D-38). `docker/compose.yaml` holds the definition and the root `docker-compose.yml` only includes it (D-41); every variable has a default, so it also works without `.env`.
- The FastAPI `TestClient` needs the `httpx2` dev dependency, not `httpx` (D-44).
