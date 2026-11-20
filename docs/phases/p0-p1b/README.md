# P0, P1a and P1b: foundations, ingestion and the offline pipeline

> **This summary was reconstructed.** There are no plan or spike documents for these three phases: they were
> built before the practice of keeping plans (the method was written down in `CLAUDE.md` only after P1c).
> It was put together on 2026-11-20 from `SPEC.md` §13, `DECISIONS.md` (D-36 to D-66), the pull request
> descriptions on GitHub and `git log`. Nothing here comes from a plan. The PDF summary of P1a to P1c that
> the owner has (`resumen p1a p1b p1c.pdf`, 2026-11-16) is not in the repository.

## Goal

Build everything that can be built and tested **without the real GitHub and without OpenAI**: the repository
skeleton and its quality gates (P0), a signed webhook in front of a Postgres job queue and a worker (P1a), and
the whole offline path from a raw job log to a redacted excerpt, an error signature, a rule-based diagnosis, an
LLM path tested with a fake model, a safe renderer and an injection suite (P1b).

## Pull requests

| PR | Date (2026) | What it did |
|---|---|---|
| (direct commits) `d716912` … `c42a95c` | Nov 12 to 13 | The specification, `DECISIONS.md` (D-01 to D-45), the P0 skeleton (uv, ruff, mypy strict, pytest, `core/` import test, pre-commit, Compose with pgvector, CI), `CLAUDE.md`. |
| #1 | Nov 13 | Switched the production target from GCP to AWS (D-46). Docs only; also the first PR, so it closed the P0 boxes "CI green on a pull request". |
| #2 P1a-1 | Nov 13 | The whole §6 schema as hand-written Alembic SQL (D-50), settings, the Postgres and in-memory `JobQueue` with one contract suite, the worker (leases, heartbeat, full-jitter backoff, drain on shutdown). D-47 to D-52. |
| #3 P1a-2 | Nov 13 | `POST /webhooks/github` (signature, size cap, ordered checks, dedupe in one transaction, 503 within 5 s), `GET /ready`, installation tracking through a `sync_installation` job, the load script. D-53 to D-56. |
| #4 P1b 1/3 | Nov 14 | `core/logs/` (job selection, 20 MB tail, step slicing, cleaning, excerpt with a 6,000-token budget shared by water-filling), `core/redaction.py`, `core/signature.py`, the `TokenCounter` port. D-57 to D-59. |
| #5 P1b 2/3 | Nov 14 | The 29 rules of SPEC §10 and their engine, `core/analysis.py`, the eval runner (`python -m evals`), and the source of the lab repository (23 failing workflows). D-60, D-61. |
| #6 | Nov 14 | Fix found by the first real `evals capture`: `gh api` refuses logs with terminal escape sequences; the capture now strips them (D-62). |
| #7 | Nov 14 | 24 eval cases captured from real lab runs, a redaction fix they exposed (GitHub's own `***` mask was being redacted), and the finding that a job-level timeout ends `cancelled` (SPEC §14, solved in P1c (f)). |
| #8 P1b 3/3 | Nov 16 | The LLM path: policy (§9.4), prompt with a nonce-delimited untrusted block, strict schema, budget reservation, one repair, fallbacks that never raise, the OpenAI client, the safe renderer, the injection suite (16 cases), the eval variants `llm` and `llm+hints`. D-63 to D-66. |
| #33, #36 (later) | Nov 18 | Once an OpenAI key existed: the live smoke and the mini-eval chose `gpt-6-luna` (D-88), and the live injection suite passed with 0 attacks won. |

## Decisions produced

- **D-46** AWS instead of GCP. **D-47** leases fenced by attempt. **D-48** the queue takes time from an injected `Clock`.
  **D-49** worker policy for outcomes, unexpected errors and shutdown. **D-50** migrations are hand-written SQL.
  **D-51** Postgres tests use their own databases and skip locally but fail in CI. **D-52** Windows needs a selector
  event loop for async psycopg and `127.0.0.1` instead of `localhost`.
- **D-53** `/ready`. **D-54** installation events are applied by the worker. **D-55** the API receives its
  dependencies. **D-56** the webhook policy: check order, what is stored, normalised job payloads (never raw
  GitHub JSON).
- **D-57** token counting is a port; the budget is water-filled. **D-58** `core` selects the failed jobs and step.
  **D-59** redaction order and idempotence, length-aware entropy, a generated secret corpus (no secret-shaped
  literal is ever committed). **D-60** rule engine details. **D-61** the eval runner and the lab. **D-62** captured
  logs are stripped of terminal controls.
- **D-63** the OpenAI call (Responses API, strict schema, `store=false`). **D-64** every diagnosis text is
  untrusted; the renderer and the oracle of the injection suite. **D-65** how LLM evals work and how the model is
  chosen. **D-66** the diagnosis pipeline: budget, repair, fallbacks.

## What was verified (SPEC §13, boxes ticked with evidence)

- **P0:** `uv sync`, Compose, pre-commit, a failing test rejected by the hook, the `core/` import test, CI green on
  `main` and on PR #1. Python 3.13 with fastembed was confirmed later, in the P2 spike.
- **P1a:** the schema from `alembic upgrade head`; a wrong signature gives 401 and no rows; the same delivery three
  times gives one row and one job; the load script (p99 109 to 139 ms for 100 deliveries); a worker killed
  mid-job is re-leased and the job completes once; transient errors back off and end `dead`. The half "exactly one
  check run" waited for P1c.
- **P1b:** the excerpt and the signature on 25 cases captured from real runs of the lab; the redaction corpus; the
  rules with positive and negative fixtures (accuracy 92.0% on the lab, the two misses are the `flaky` cases by
  design); the fake-LLM behaviours; the injection suite offline. The live smoke, the model choice and the live
  injection suite came on 2026-11-18 (D-88).

## What stayed open, and why

At the end of P1b nothing talked to OpenAI because there was no key: the live smoke, the model choice and the
live injection suite were written, skipped and unproven. They were closed in P2 time (PRs #33 and #36).

## What the method taught (reconstructed, only what the PR descriptions record)

- **The first real data corrects the offline assumptions.** The lab capture exposed an escape-sequence refusal
  (#6), a redaction rule that ate GitHub's own mask (#7) and the fact that a job-level timeout is not a log line
  but an annotation of the check run (#7, solved in P1c). None of these could have been found with synthetic logs.
- **An oracle that does not call the code it judges** found four real gaps in the renderer while it was written
  (#8); and removing nine defences one at a time, each making a test fail, is how the injection suite proves that
  it tests something.
- **A forced category is the one thing no check in `core` can decide:** an obedient fake model forces it by
  design, so only the live suite is evidence. That is why the box was left open until a key existed.
- Windows details cost time early (D-52, D-55); writing them as decisions saved the later phases from rediscovering them.
