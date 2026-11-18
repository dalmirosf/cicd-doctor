# CICD Doctor — Decision log

Each entry records **what** was decided, **why**, the main **alternatives** considered, and the **consequences** (including when to revisit). [SPEC.md](SPEC.md) references these entries as `D-NN`.

**Origin** says where each decision came from:
- **Brief**: the author's initial design brief.
- **Interview**: chosen by the author during the design interview.
- **P0 request**: asked for by the author in the Phase 0 skeleton request, where it differs from the spec as first written.
- **Author request**: asked for by the author in a later request, after the spec and the Phase 0 skeleton existed.
- **Proposed**: a conventional default proposed while writing the spec, accepted with it, or a choice made while implementing a phase and reported to the author. These are the first ones to challenge.

New decisions are appended. A superseded decision is marked `Superseded by D-NN` and kept.

---

## Index

| ID | Decision | Origin |
|---|---|---|
| D-01 | GitHub Actions only, triggered by `workflow_run` + `/cicd-doctor explain` | Brief |
| D-02 | Hexagonal architecture, enforced with import-linter | Brief + Proposed |
| D-03 | Pydantic is allowed inside `core/` | Proposed |
| D-04 | FastAPI webhook that only enqueues + separate worker | Brief |
| D-05 | Postgres as the job queue (`SKIP LOCKED`) | Interview |
| D-06 | Three-level idempotency | Brief + Proposed |
| D-07 | OpenAI with strict Structured Outputs; model chosen by mini-eval | Interview + Proposed |
| D-08 | Local embeddings with fastembed (bge-small, 384-d) | Interview |
| D-09 | Error signature = normalized canonical text → SHA-256 + embedding | Interview |
| D-10 | Cache and similarity scoped to one repository | Proposed |
| D-11 | Log limits: 3 failed jobs, 20 MB tail per job, ~6k tokens | Interview + Proposed |
| D-12 | Redact before storage, LLM and publishing; never store raw logs | Brief + Proposed |
| D-13 | Rule baseline: generic GHA + Python + Docker + Terraform/GCP. Amended by D-46 (Terraform/AWS) | Interview |
| D-14 | Rule matches are passed to the LLM as hints; evals ablate them | Proposed |
| D-15 | Fixed 8-category enum; `flaky` from a single log is only "suspected" | Brief + Proposed |
| D-16 | Cost control: signature cache + per-call, per-installation and global caps | Interview |
| D-17 | Fork PRs: rules automatically, LLM only on maintainer request | Interview |
| D-18 | Output: neutral check run + one sticky comment per (PR, workflow). Amended by D-76 (check run named `CICD Doctor: <workflow>`) | Interview |
| D-19 | Fixes are suggestions only; suggested changes only on diff lines | Brief + Proposed |
| D-20 | Auto-fix PRs live in a separate opt-in "Fixer" app | Interview |
| D-21 | Minimal GitHub App permissions, per-repo scoped tokens | Proposed |
| D-22 | Command authorization by `author_association` + rate limit | Proposed |
| D-23 | Error classification, retries, leases and dead jobs | Proposed |
| D-24 | Flaky = same SHA passes on a later attempt; fix = first green SHA on the same branch | Brief + Proposed |
| D-25 | Prompt-injection defense in depth | Brief + Proposed |
| D-26 | Repo config read only from the default branch | Proposed |
| D-27 | MVP = end-to-end slice without memory features | Interview |
| D-28 | Library choices | Proposed |
| D-29 | Quality tooling and gates | Brief + Proposed |
| D-30 | Two GitHub Apps (dev via smee.io, prod) | Brief |
| D-31 | GCP deployment with Terraform. Superseded by D-46 | Brief + Proposed |
| D-32 | Observability: structlog JSON + Langfuse | Brief + Proposed |
| D-33 | Evals: committed fixtures, cached LLM responses, LLM judge, CI gate | Brief + Proposed |
| D-34 | Data retention: 30 days deliveries, 90 days failures | Proposed |
| D-35 | Store both the rules and the LLM diagnosis for every failure | Proposed |
| D-36 | English for code, commits, README and design docs | Brief + Interview |
| D-37 | Local Kubernetes is an optional extra | Brief |
| D-38 | Local Postgres is published on host port 5433 | Proposed |
| D-39 | `api/` and `worker/` are sibling packages; `app/` is only the composition root | P0 request |
| D-40 | The liveness endpoint is `GET /health` | P0 request |
| D-41 | Compose lives in `docker/compose.yaml`, included by a root `docker-compose.yml` | P0 request |
| D-42 | Pre-commit runs ruff and the fast tests; mypy runs in CI | P0 request |
| D-43 | The `core/` import rule is enforced by a pytest test, not import-linter | P0 request |
| D-44 | `httpx2` backs the FastAPI test client | Proposed |
| D-45 | CI runners are pinned to `ubuntu-24.04` instead of `ubuntu-latest` | Author request |
| D-46 | AWS deployment with Terraform (replaces GCP; amends D-13) | Author request |
| D-47 | Job leases are fenced by attempt; `heartbeat` and `wait_for_jobs` join the `JobQueue` port | Proposed |
| D-48 | The queue takes time from the injected `Clock`, not from SQL `now()` | Proposed |
| D-49 | Worker execution policy: outcomes, unexpected errors, backoff, shutdown | Proposed |
| D-50 | Migrations are hand-written SQL; Alembic lives in the Postgres adapter | Proposed |
| D-51 | Postgres tests: own databases, "integration wins", skip locally, fail in CI | Proposed |
| D-52 | Windows: selector event loop for psycopg async, and `127.0.0.1` instead of `localhost` | Proposed |
| D-53 | The readiness endpoint is `GET /ready` | Author request |
| D-54 | Installation events are applied by the worker through a `sync_installation` job | Author request + Proposed |
| D-55 | The API receives its dependencies; uvicorn starts it from the composition root | Proposed |
| D-56 | Webhook request policy: check order, what is stored, timeouts, normalised payloads | Proposed |
| D-57 | Token counting goes through a `TokenCounter` port; tiktoken lives in an adapter; the budget is water-filled | Proposed |
| D-58 | `core` selects the failed jobs and step; line numbers count in the retained log | Author request + Proposed |
| D-59 | Redaction and signature details: order, thresholds, PEM spans, generated corpus, fixed-point normalisation | Proposed |
| D-60 | Rule engine: per-rule priorities, context conditions, one match per rule, `core/analysis.py` | Proposed |
| D-61 | Eval runner skeleton, lab repository sources and the `evals capture` command. Amended by D-62 | Author request + Proposed |
| D-62 | Captured logs are stripped of terminal escape sequences; `gh` reads them with `--allow-escape-sequences` | Author request + Proposed |
| D-63 | OpenAI call: Responses API, strict schema without length keywords, `store=false`, retries owned by the pipeline | Proposed |
| D-64 | Safe rendering treats every diagnosis text as untrusted; a CommonMark-aware threat scanner and `judge` define an attack; the injection suite is synthetic | Proposed |
| D-65 | LLM evals: the pipeline is the system under test, answers are cached, `-m live` runs on demand, the model is chosen by a mini-eval that waits for the key | Author request + Proposed |
| D-66 | Diagnosis pipeline: budget reservation, one repair, fallbacks that never raise, evidence verification and restricted suggestions | Proposed |
| D-67 | GitHub HTTP client: `httpx2`, one error mapping, tests on `httpx2.MockTransport` with a FakeGitHub (no respx). Amends D-28 and D-44 | Proposed |
| D-68 | App authentication: PyJWT, installation tokens scoped per repository and permission, in-memory cache with one request in flight | Proposed |
| D-69 | Postgres store and ledger: one checkpoint per run attempt, deterministic failure ids, the LLM usage on the published diagnosis row | Proposed |
| D-70 | The boundary between persisting and publishing: compute once, publish from what is stored, exactly one comment | Proposed |
| D-71 | Log download: a redirect fetched without credentials, an offset `Range`, a ring buffer behind it | Proposed |
| D-72 | The pull request of a run, forks included: only PRs whose base is this repository, else a search by head | Proposed |
| D-73 | Job-level timeouts are diagnosed: `cancelled` runs are enqueued, and a cancelled job whose annotations report the timeout is treated as `timed_out`; any other cancellation stays silent | Proposed |
| D-74 | The handler of `diagnose_run` publishes "Could not diagnose" when the job gives up (permanent error or last attempt), then re-raises | Proposed |
| D-75 | The `diagnose_run` payload carries the account; the use case makes sure the installation and repository rows exist | Proposed |
| D-76 | The check run is named `CICD Doctor: <workflow>`, with the name sanitised and cut. Amends D-18 | Proposed |
| D-77 | The embedding is computed in the use case before the LLM and travels in the checkpoint; an embedder error degrades, it never fails the job | Proposed |
| D-78 | The model lives in the worker process, warms up in the background, caches in a fixed directory and has a switch | Proposed |

---

## D-01 — GitHub Actions only, triggered by `workflow_run` + `/cicd-doctor explain`
**Origin:** Brief
**Decision:** Diagnose on `workflow_run` with `action = completed` and `conclusion ∈ {failure, timed_out}`. Also offer a manual trigger by commenting `/cicd-doctor explain` on a PR (`issue_comment`). Successful `workflow_run` events are consumed too, but only to record outcomes (D-24).
**Why:** `workflow_run` fires once per run attempt with everything needed (run id, attempt, head SHA, PRs). The app doesn't need to touch the user's workflows: no action to add, no YAML changes. `timed_out` is included because a timeout is one of the categories. The comment command covers the cases where automatic LLM use is off (forks, `on_demand` config).
**Alternatives:** `check_suite`/`check_run` events (noisier, they also include non-Actions checks); a GitHub Action users add to their workflow (runs with the user's token and minutes, and needs a YAML change in every repo); `workflow_job` events (per job, which multiplies deliveries and loses the run-level view).
**Consequences:** Only GitHub Actions is supported. Each re-run attempt is a separate event, which is exactly what flaky detection needs.

## D-02 — Hexagonal architecture, enforced with import-linter
**Origin:** Brief (architecture) + Proposed (enforcement)
**Decision:** `core/` holds the models, the domain logic and the use cases, and depends only on ports (`Protocol`s): `LogSource`, `Embedder`, `FailureStore`, `LLMClient`, `Reporter`, `JobQueue`, plus `RepoConfigSource`, `UsageLedger` and `Clock`. Adapters (GitHub, OpenAI, fastembed, Postgres, in-memory) implement the ports. `app/` is the composition root. An import-linter contract forbids `core` from importing `adapters`, `app`, or IO/framework libraries (`fastapi`, `httpx`, `sqlalchemy`, `psycopg`, `openai`, `fastembed`). It runs in pre-commit and CI.
**Why:** The interesting logic (excerpting, redaction, rules, validation, policy) is pure and becomes trivially testable with in-memory adapters. Providers can be swapped (D-07, D-08) without touching the domain. The rule is checked automatically instead of relying on discipline.
**Alternatives:** Layered "service + repository" without explicit ports; checking the rule only in code review.
**Consequences:** Some boilerplate (ports + two implementations of the stores). The extra ports (`RepoConfigSource`, `UsageLedger`, `Clock`) exist so that config, budget and time stay testable.
**Amended by D-43:** the import rule is enforced by a pytest test instead of import-linter. **Amended by D-39:** `api/` and `worker/` are sibling packages of `app/`, and `core/` must not import them either.

## D-03 — Pydantic is allowed inside `core/`
**Origin:** Proposed
**Decision:** Domain models are frozen Pydantic v2 models, and `core/` may import `pydantic`.
**Why:** Validating the LLM JSON is a core responsibility (D-25), and Pydantic generates the JSON Schema for OpenAI Structured Outputs from the same model, so the contract has a single source of truth. Pydantic does no IO, so it doesn't break the hexagon's purpose.
**Alternatives:** Plain dataclasses in core + Pydantic DTOs in adapters (duplicated models and mapping code); attrs + jsonschema.
**Consequences:** Core depends on one third-party library, explicitly allowed in the import contract.

## D-04 — FastAPI webhook that only enqueues + separate worker
**Origin:** Brief
**Decision:** The API verifies the signature, records the delivery, enqueues a job and returns 202. A separate worker process does all GitHub, LLM and embedding work.
**Why:** GitHub cancels a delivery after 10 s, while a diagnosis (log download + LLM) can take far longer. Keeping slow calls out of the request path makes the acknowledgement fast (target p99 < 1 s) and lets work be retried independently of the delivery.
**Alternatives:** FastAPI `BackgroundTasks` (lost on restart, no retries); synchronous processing (breaks the 10 s limit).
**Consequences:** Two processes to run and deploy (D-31). Delivery-to-diagnosis latency depends on the worker's queue (NFR-02).

## D-05 — Postgres as the job queue (`SKIP LOCKED`)
**Origin:** Interview
**Decision:** A `jobs` table, leased with `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`, with `locked_until` leases, `attempts`/`max_attempts`, `run_after` for backoff, `LISTEN/NOTIFY` to wake workers, and a 5 s poll fallback.
**Why:** Postgres is already required (pgvector). No extra service in Compose or GCP; Memorystore would add about USD 35/month minimum to a portfolio project. Recording the delivery id and enqueueing the job can happen in **one transaction**, which makes idempotency atomic (D-06). The expected load (≤ 1k diagnoses/day, NFR-11) is far below what this pattern handles.
**Alternatives:** Redis + arq/RQ (more infra, and idempotency split across two stores); Cloud Tasks in prod with Postgres locally (fits serverless better, but needs two adapters and behaves differently in dev and prod); a library such as procrastinate (viable, but a hand-written queue of about 100 lines is easier to explain and test, and shows the pattern).
**Consequences:** The worker must run continuously (D-31). Revisit if load exceeds about 10 jobs/s or if Cloud Run's always-on cost becomes a problem (then consider Cloud Tasks behind the same `JobQueue` port).
**Amended by D-47 and D-48:** leases are fenced by attempt and extended by heartbeats, and the queue takes its time from the injected `Clock`.

## D-06 — Three-level idempotency
**Origin:** Brief (delivery id) + Proposed (the other two levels)
**Decision:**
1. `webhook_deliveries.delivery_id` (`X-GitHub-Delivery`) has a primary key; a duplicate is a 200 no-op.
2. `jobs.dedupe_key` (`diagnose:{run_id}:{attempt}`) is unique, so two different deliveries about the same attempt (for example, a manual redelivery) produce one job.
3. Side effects are keyed by stored ids: one check run per `(repo, run_id)` and one comment per `(repo, PR, workflow)`, each with a `last_attempt` guard so an older attempt never overwrites a newer one. The comment also carries a hidden marker so it can be found if the stored id is lost.

**Why:** The queue is at-least-once (leases can expire mid-job). Only idempotent effects make that safe, and the brief requires comments that are updated, never duplicated.
**Alternatives:** Exactly-once processing (not achievable across GitHub API calls); idempotency only at the delivery level (does not protect against worker crashes).
**Consequences:** Two publication tables and a comparison on every publish.

## D-07 — OpenAI with strict Structured Outputs; model chosen by mini-eval
**Origin:** Interview (provider) + Proposed (model selection)
**Decision:** The `LLMClient` adapter uses the OpenAI SDK with Structured Outputs (`json_schema`, `strict: true`) generated from the Pydantic output model, and the response is re-validated in core. The model id is configuration (`OPENAI_MODEL`). In P1, two small/cheap-tier candidates are compared on the lab fixtures (accuracy, cost, latency), the winner is recorded here as a new entry, and its price goes in the settings for cost accounting. One call per failed job; no automatic escalation to a larger model in v1.
**Why:** OpenAI was the author's choice. Strict Structured Outputs nearly removes malformed JSON, and the re-validation keeps the core independent of that guarantee. Choosing the model by a measured eval, instead of naming one now, avoids hard-coding an id or price that may change and turns the choice into evidence for the portfolio.
**Alternatives:** Anthropic Claude, Gemini on Vertex (everything inside GCP, no API key to store), several providers from v1 (more work early). The `LLMClient` port keeps all of them possible later as an eval variant.
**Consequences:** One more secret (`OPENAI_API_KEY`) in Secret Manager. Escalating to a stronger model on low confidence is a candidate for after P4 if the evals justify its cost.

## D-08 — Local embeddings with fastembed (bge-small, 384-d)
**Origin:** Interview
**Decision:** `BAAI/bge-small-en-v1.5` through fastembed (ONNX, CPU) inside the worker, producing 384-d vectors stored in `vector(384)` with an HNSW cosine index. The model is baked into the Docker image.
**Why:** Zero marginal cost. Deterministic output, which keeps tests and evals reproducible. Redacted excerpts don't go to a second provider. 384 dimensions keep the index small. Signature texts are short and normalized (D-09), so a small model is enough.
**Alternatives:** Voyage `voyage-code-3` (strong on code, but another key and network dependency), OpenAI `text-embedding-3-small` (cheap, but another network hop), Vertex embeddings.
**Consequences:** A larger image (~150 MB more), more RAM, and a slower cold start for the worker. Changing the model later means re-embedding and a migration of the vector size. The `Embedder` port isolates that.

## D-09 — Error signature = normalized canonical text → SHA-256 + embedding
**Origin:** Interview
**Decision:** The canonical text is: workflow name, job name without the matrix suffix, step name, error type (top rule id, else the exception class), and the first 5 normalized error lines. Normalization replaces timestamps, durations, UUIDs, hex runs, IPs, ports and integers with placeholders, makes workspace paths relative and drops query strings. `sha256(canonical)` gives exact matches; the embedding of the same text gives similarity.
**Why:** Deterministic, cheap and testable with property tests. One definition of "same error" serves both the exact cache and the vector search. Leaving out the matrix suffix groups the same failure across OS or Python versions.
**Alternatives:** Drain3-style template mining (more robust to odd variations, but stateful and harder to test); an LLM-written summary as the signature (non-deterministic, costs a call, and puts the LLM before the cache).
**Consequences:** Normalization rules need maintenance as new log formats appear; golden tests guard them. Errors that differ only in a normalized number (for example, different ports) collapse into one, which is the intended behavior.

## D-10 — Cache and similarity scoped to one repository
**Origin:** Proposed
**Decision:** Signature-cache lookups and vector searches always filter by `repository_id`.
**Why:** A diagnosis contains excerpts of a private repository's logs. Matching across repositories, even inside one installation, could show one repo's content in another repo's PR. Scoping by repository makes leaks impossible by construction, and an isolation test checks it.
**Alternatives:** Per-installation scope (more hits, but leaks across repos of an organization); a global anonymized knowledge base (much more complex, and a privacy risk).
**Consequences:** A new repository starts with an empty memory. Revisit an opt-in per-installation scope if the evals show a large gain.

## D-11 — Log limits: 3 failed jobs, 20 MB tail per job, ~6k tokens
**Origin:** Interview (limits) + Proposed (tail strategy)
**Decision:** Only failed jobs are fetched, through `/actions/jobs/{id}/logs` (not the run-wide zip), at most 3, earliest failure first. At most 20 MB is retained per job, **tail-biased**: an HTTP `Range` request for the last 20 MB when the size is known, otherwise a 20 MB ring buffer with a hard stop at 100 MB read. The excerpt is the failed step's last 150 lines plus ±15 lines around each error match, deduplicated, with a total of ~6,000 tokens (tiktoken `o200k_base`) across jobs.
**Why:** The root cause is almost always in the failed step and near its end. These limits bound cost and latency and reduce the injection surface. The refinement: the option chosen in the interview said "cut the stream" at 20 MB, but keeping the *first* 20 MB would lose the error at the end of large logs. Keeping the tail preserves the intent (bounded memory and transfer) without that failure mode.
**Alternatives:** The whole run zip up to 100 MB with ~24k tokens (4–5× the cost); a minimal 80 lines / 2k tokens (misses root causes that appear early).
**Consequences:** Failures whose real cause is in an earlier, successful step (for example, a warning during setup) may be missed. The evals measure this.

## D-12 — Redact before storage, LLM and publishing; never store raw logs
**Origin:** Brief (redact before the LLM) + Proposed (the rest)
**Decision:** Redaction (GitHub, AWS, GCP and other provider tokens, JWTs, PEM blocks, URL credentials, auth headers, high-entropy `key=value`) is part of building the excerpt, so the redacted excerpt is the only form stored or sent. A second pass runs on the LLM output before publishing, and a third in the logging pipeline. Raw logs exist only in worker memory during one job.
**Why:** GitHub masks only registered secrets, not tokens generated at runtime or printed by tools. Storing raw logs would make the database a high-value target and complicate retention.
**Alternatives:** Redacting only before the LLM (the database and our logs would still hold secrets); storing raw logs encrypted (still a liability, and not needed for diagnosis).
**Consequences:** Some false positives (redacted non-secrets) are accepted. Re-diagnosing an old failure with a new prompt uses the stored redacted excerpt, which is also what the LLM saw.

## D-13 — Rule baseline: generic GHA + Python + Docker + Terraform/GCP
**Amended by D-46:** the cloud rule family is AWS (`AWS-*`) instead of GCP (`GCP-*`).
**Origin:** Interview
**Decision:** v1 rules cover generic GitHub Actions failures (timeout, OOM/137, lost runner, disk full, DNS/connection, rate limit, missing input/secret) plus Python (pip/uv, pytest, ruff, mypy), Docker build/push and Terraform/GCP. Lint and type-check failures map to `broken_test` with a rule id that names the gate (`PY-RUFF`, `PY-MYPY`).
**Why:** These ecosystems match the project's own stack, so the app can diagnose its own CI and CD, and the lab repo's failures. Node/JS was left out of v1 to keep scope tight. The 8 categories come from the brief and have no "code quality" bucket. `broken_test` is the closest ("the code fails its own checks"), and the rule id keeps the distinction visible.
**Alternatives:** Adding Node/JS now; adding a `code_quality` category (changes the brief's taxonomy).
**Consequences:** Failures in other ecosystems get `unknown` from the rules and rely on the LLM. Revisit a `code_quality` category after the P4 confusion matrix.

## D-14 — Rule matches are passed to the LLM as hints; evals ablate them
**Origin:** Proposed
**Decision:** In production, rule matches (rule id, category, matched line numbers) go into the trusted section of the prompt. The evals run both `llm` (no hints) and `llm+hints`.
**Why:** Hints are cheap, precise context that should raise accuracy on known patterns. Without the ablation, rules vs LLM would not be a fair comparison, so both variants are measured.
**Alternatives:** Never giving hints (a "purer" comparison, likely worse in production); giving hints only when the rule confidence is high.
**Consequences:** A wrong high-confidence rule could anchor the LLM. The disagreement display (SPEC §9.3) and the evals keep that visible.

## D-15 — Fixed 8-category enum; `flaky` from a single log is only "suspected"
**Origin:** Brief (categories) + Proposed (flaky semantics)
**Decision:** The categories are `broken_test`, `dependency`, `network`, `config_or_secrets`, `timeout`, `infra_or_deploy`, `flaky`, `unknown`. When the LLM says `flaky`, it means a suspected flaky failure. Confirmed flakiness is the failure status `resolved_flaky` (D-24). Rules never output `flaky`.
**Why:** Flakiness is a property of history (same code, different result), not of one log. Keeping the two separate stops the model from guessing a label that the data can confirm later.
**Alternatives:** Removing `flaky` from the LLM categories (but there are valid signals, such as known-flaky test names or race-condition traces).
**Consequences:** The evals score the `flaky` category and flaky *detection* separately.

## D-16 — Cost control: signature cache + per-call, per-installation and global caps
**Origin:** Interview
**Decision:**
- Reuse the LLM diagnosis of an identical `signature_hash` in the same repository from the last 7 days (source `cache`, P2).
- At most 1 LLM call per failed job per attempt (plus at most one schema-repair call), and `LLM_MAX_OUTPUT_TOKENS` = 800.
- 30 LLM diagnoses per installation per UTC day.
- A global daily budget, `LLM_DAILY_BUDGET_USD` = 2.00, enforced by reserving the estimated maximum cost before each call (under an advisory lock) and settling the real cost after.

Over any limit, the rules diagnosis is published with `fallback_reason = budget`, and the check run says so.
**Why:** It bounds the worst case (a noisy installation or a fork spam loop) without degrading the normal case, and repeated failures, the most common pattern, cost nothing. Reservation makes the cap hold under concurrent workers.
**Alternatives:** Only a global monthly budget (one installation can starve the rest); no limits in v1 (not acceptable once the app is public).
**Consequences:** A busy day may fall back to rules. The limits are configuration, and the evals provide the real cost per diagnosis to tune them.

## D-17 — Fork PRs: rules automatically, LLM only on maintainer request
**Origin:** Interview
**Decision:** For runs whose head repository differs from the base repository, the automatic path publishes the rules diagnosis only (`fallback_reason = fork_policy`). The LLM runs only when an authorized user comments `/cicd-doctor explain` (D-22).
**Why:** Fork PRs are the main source of untrusted log content (prompt injection) and of abusive volume (cost). Rules are free and immune to injection. Maintainers keep a one-command path to the full diagnosis.
**Alternatives:** Treating forks like any other run (best contributor experience, most exposure); ignoring forks entirely (loses the open-source use case).
**Consequences:** Contributors from forks see a weaker diagnosis by default. The check text explains why and how to get more.

## D-18 — Output: neutral check run + one sticky comment per (PR, workflow)
**Origin:** Interview
**Decision:** Always create a check run named `CICD Doctor: <workflow>` (amended by D-76; it was plain `CICD Doctor`) on the run's `head_sha` (`in_progress` at start, `completed` with conclusion `neutral`). When the run has a PR, also create one comment per (PR, workflow), identified by a hidden marker and by its stored id, and edit it on re-runs, new failing pushes, and resolution. Comments can be turned off per repo (`comment: false`, P3).
**Why:** The check run is visible for every run, including pushes to `main` without a PR, and holds the full report. The comment brings the conclusion to where reviewers look. `neutral` ensures the doctor never blocks merges or adds a second red cross to a failure that is already red. One comment per workflow keeps parallel workflows from overwriting each other, while still avoiding duplicates.
**Alternatives:** Check run only (less visible); comment only (no output for runs without a PR); one comment per PR covering all workflows (concurrent updates from different workflows would conflict).
**Consequences:** A PR with several failing workflows gets several comments, one per workflow, each updated in place.
**Amended by D-76:** the check run is named `CICD Doctor: <workflow>`, so two failing workflows on one commit give two check runs that can be told apart; the sticky comment carries the workflow in its heading for the same reason.

## D-19 — Fixes are suggestions only; suggested changes only on diff lines
**Origin:** Brief (suggestions only) + Proposed (diff-line rule)
**Decision:** The main app never changes code. A proposed change becomes a GitHub "suggested change" (a review comment with a `suggestion` block) only when its line range lies inside the PR diff, the path is relative without `..`, and the span is ≤ 30 lines. Otherwise it is shown only as text.
**Why:** GitHub accepts review comments only on diff lines, and a suggestion is applied by a human with one click, so a person is always in the loop. The limits on path and span prevent odd or large edits produced by injected content.
**Alternatives:** Posting patches as text only (less useful); opening fix PRs from the main app (rejected, D-20).
**Consequences:** Many root causes (for example, a missing dependency in an untouched `pyproject.toml`) can only be shown as text.

## D-20 — Auto-fix PRs live in a separate opt-in "Fixer" app
**Origin:** Interview
**Decision:** Automatic fix PRs (optional, off by default, low-risk only, never auto-merged) are delivered in P6 by a second GitHub App, "CICD Doctor Fixer", with `contents: write`. The main app keeps `contents: read`.
**Why:** It is real least privilege. Installing the diagnoser never grants write access to code, and only users who want auto-fix install the second app. A feature flag inside one app would still make every installation grant write access.
**Alternatives:** One app with `contents: write` and a feature flag; dropping auto-fix entirely.
**Consequences:** Two apps to register and operate. The Fixer allowlist (SPEC P6) must stay narrow, and it never runs code.

## D-21 — Minimal GitHub App permissions, per-repo scoped tokens
**Origin:** Proposed
**Decision:**

| Permission | Level |
|---|---|
| Actions | read |
| Checks | write |
| Contents | read |
| Metadata | read |
| Pull requests | write |

Events: `workflow_run` and `issue_comment`. No Issues, Administration or Workflows permission. Installation tokens are requested with `repository_ids: [repo]` and only the permissions the use case needs, and cached until 5 minutes before expiry.
**Why:** Each permission maps to a feature (SPEC §9.6). `contents: read` is needed for the default-branch config, compare for fix tracking, and file context for suggestions. Comments on PRs, review comments and reactions are covered by Pull requests write. Narrowing each token reduces the impact of a leaked token.
**Alternatives:** Issues write for comments (broader, since it also covers non-PR issues); skipping `contents: read` (would remove the config file and fix diffs).
**Consequences:** **To verify in P3:** whether `issue_comment` events on PRs are delivered with only the Pull requests permission. If not, add Issues: read and update this entry.

## D-22 — Command authorization by `author_association` + rate limit
**Origin:** Proposed
**Decision:** `/cicd-doctor explain` is accepted only on PRs, only as the first line of the comment, only from `OWNER`, `MEMBER` or `COLLABORATOR`, and at most once per PR per 5 minutes. Accepted commands get a 👀 reaction; rejected ones are ignored silently.
**Why:** The command bypasses the fork policy (D-17), so only trusted users may use it. `author_association` comes in the payload and needs no extra API call or permission. The rate limit caps cost even for trusted users. Silent rejection avoids noise and doesn't reveal the policy.
**Alternatives:** Checking the collaborator permission through the API (more precise, but costs a call); allowing the PR author to use it (it would reopen the fork cost and injection path).
**Consequences:** Outside collaborators with triage-only roles may be rejected. Acceptable for v1.

## D-23 — Error classification, retries, leases and dead jobs
**Origin:** Proposed
**Decision:**
- Errors are `TransientError` (GitHub 5xx, network, timeouts, DB, 429 or secondary rate limit) or `PermanentError` (403, 404/410 for expired logs, a suspended installation, validation errors).
- Transient errors are retried up to 5 attempts with exponential backoff and full jitter (base 30 s, ×4, max 30 min), honoring `Retry-After` and `x-ratelimit-reset`.
- Permanent errors mark the job `failed` and publish a neutral "Could not diagnose" check when possible. Exhausted jobs become `dead` and are logged at ERROR.
- Leases last 300 s, so jobs from crashed workers are picked up again.
- LLM failures never retry the job: 2 retries inside the job, 1 schema-repair attempt, then the rules fallback.

**Why:** Retrying permanent errors wastes rate limit and delays the user. LLM problems should degrade the answer (rules), not delay it. Leases plus idempotent effects (D-06) make crashes harmless.
**Alternatives:** Uniform retries for every error; a separate dead-letter table (the job status already serves that purpose).
**Consequences:** GitHub does not retry failed webhook deliveries automatically, so if the API cannot write to the database it answers 503, and the runbook explains how to redeliver from the app settings.
**Amended by D-49:** exceptions that are neither transient nor permanent are retried like transient ones, and `TransientError.retry_after` replaces the computed backoff.

## D-24 — Flaky = same SHA passes on a later attempt; fix = first green SHA on the same branch
**Origin:** Brief (definitions) + Proposed (exact matching)
**Decision:** On a successful `workflow_run`:
- If an earlier attempt of the **same run** (same `head_sha`) failed, its failures become `resolved_flaky`.
- Otherwise, open failures of the same workflow on the same branch key (`head_repo_full_name` + `head_branch`) at a different SHA become `resolved_by_commit`, with `fix_commit_sha` set to the successful SHA.
- Open failures with no resolution after 30 days become `stale`.

In every case, the sticky comment is edited to show the resolution.
**Why:** Same code with a different result is the operational definition of flaky given in the brief. The first green commit on the same branch is the best cheap estimate of the fix and links each failure to its real fix for the evals.
**Alternatives:** Comparing the commit tree to detect "no code change" across different SHAs (more precise for empty commits, but more API calls); detecting flakes statistically over many runs (needs volume).
**Consequences:** When several commits are pushed at once, the "fix" commit may contain unrelated changes; the stored compare range shows the whole span. A failure re-run on a different branch is not credited as flaky.

## D-25 — Prompt-injection defense in depth
**Origin:** Brief (requirement) + Proposed (mechanisms)
**Decision:** Logs and similar past failures go only inside a nonce-delimited `<untrusted_log>` block, and tag look-alikes inside them are escaped. The system prompt says the content is data. The model gets no tools. The output is a strict enum-based schema with length caps. Evidence must quote the excerpt, otherwise it is dropped and confidence is capped at 0.3. A rule baseline is computed independently and shown when it disagrees. Fork runs skip the LLM automatically (D-17). Output is sanitized (escaped markdown and HTML, neutralized mentions, non-allowlisted URLs shown as inert code, no images). An injection eval suite must have 0 successful attacks.
**Why:** No single defense stops prompt injection. The goal is that a successful injection can at most produce a wrong, low-confidence diagnosis: never an action, a clickable phishing link, a mention spam, or a leaked secret. That is achieved by limiting what the output *can* do, not only what the model is told.
**Alternatives:** Relying on the system prompt alone; a second "guard" LLM call (more cost and latency for limited gain, given that the output can't act).
**Consequences:** Some legitimate links in diagnoses are not clickable. The allowlist can be extended.

## D-26 — Repo config read only from the default branch
**Origin:** Proposed
**Decision:** `.github/cicd-doctor.yml` (`comment`, `llm: auto|on_demand|off`, `ignore_workflows`) is read from the default branch, parsed with `yaml.safe_load`, validated with Pydantic (unknown keys rejected), and replaced by defaults when it is missing or invalid.
**Why:** If the config were read from the PR head, a fork PR could enable the LLM for itself or change the behavior. The default branch is controlled by maintainers.
**Alternatives:** Reading from the head SHA (lets authors test config in a PR, but is unsafe); configuration in a web UI (out of scope).
**Consequences:** Config changes apply only after they are merged.

## D-27 — MVP = end-to-end slice without memory features
**Origin:** Interview
**Decision:** v1 (P1) is the whole path: signed, idempotent webhook → Postgres queue → worker → failed job logs → cleaning and redaction → signature hash → rules + LLM (validated, with fallback, budget caps, fork policy) → neutral check run + sticky comment, running locally with smee and Compose, diagnosing its own CI. pgvector similarity, the signature cache, flaky and fix tracking, the explain command, review suggestions and repo config come in P2 and P3.
**Why:** It delivers a working, demonstrable product as early as possible, and every later feature plugs into ports that already exist. Starting with rules only would postpone the most valuable and riskiest part (the LLM contract and the injection defenses).
**Alternatives:** Slice + memory in v1 (a richer demo, but a later first release); rules only first.
**Consequences:** The `signature_hash` and the schema columns for later phases (embedding, status) exist from P1, so P2 needs no breaking migrations.

## D-28 — Library choices
**Origin:** Proposed
**Decision:**
- Python 3.13 (fall back to 3.12 if fastembed or onnxruntime lag), uv.
- FastAPI + uvicorn; Pydantic v2 + pydantic-settings.
- SQLAlchemy 2 Core (async) on psycopg 3, Alembic, pgvector-python.
- httpx + PyJWT for the GitHub API, respx in tests.
- openai SDK, tiktoken, fastembed, PyYAML (`safe_load` only).
- structlog; Langfuse SDK in P5.
- pytest, hypothesis, ruff, mypy, import-linter, pre-commit.

**Why:** These are mainstream, typed and well documented. A thin GitHub client over httpx stays transparent (the auth, redirect-without-auth, Range and rate-limit behavior are explicit and testable) instead of hiding them behind PyGithub or githubkit. Alembic gives reviewable migrations. SQLAlchemy Core (not the ORM) keeps the SQL, including `SKIP LOCKED`, explicit.
**Alternatives:** githubkit (typed and complete, but more magic); asyncpg directly (no migration story); an ORM.
**Consequences:** A bit more code in the GitHub adapter, covered by recorded-response tests.
**Amended by D-43, D-44 and D-67:** import-linter is not used (a pytest test replaces it), the FastAPI test client uses `httpx2`, and the GitHub client uses `httpx2` as a runtime dependency with `httpx2.MockTransport` in the tests, not `httpx` and `respx`.

## D-29 — Quality tooling and gates
**Origin:** Brief (tools) + Proposed (configuration)
**Decision:**
- ruff for lint and format, `mypy --strict`, pytest with markers (`unit`, `contract`, `integration`, `e2e`, `live`), and the import-linter contracts.
- pre-commit runs ruff, mypy, import-linter and the fast suite (`unit or contract` with memory adapters, target < 30 s), so commits with broken tests are rejected.
- GitHub Actions CI runs everything, with a pgvector service container, and requires `core/` coverage ≥ 90%.
- The dev app is installed on this repo so it diagnoses its own CI.

**Why:** A fast local gate catches most problems before a push, and CI is the full authority. mypy is chosen over pyright for its established `--strict` profile and its plugin support (Pydantic).
**Alternatives:** pyright or basedpyright (faster; a reasonable switch later); running the full suite in pre-commit (too slow, so people would skip it).
**Consequences:** Integration tests run only in CI, unless the developer starts Compose and runs `-m integration`.
**Amended by D-42 and D-43:** pre-commit runs ruff and the fast tests only (mypy runs in CI), and the import contract is a pytest test, so there is no import-linter hook.

## D-30 — Two GitHub Apps (dev via smee.io, prod)
**Origin:** Brief
**Decision:** A dev app whose webhook points at a smee.io channel forwarded to localhost, and a prod app pointing at Cloud Run. Each has its own private key and webhook secret, and both use the same code (`APP_ENV`).
**Why:** Development never touches production installations or secrets, and webhooks reach a Windows laptop without opening ports.
**Alternatives:** One app with a switchable URL (risks prod traffic going to a laptop); ngrok or cloudflared tunnels (fine alternatives, but smee needs no account).
**Consequences:** Permission changes must be applied to both apps; the manifest is kept in the repo to keep them in sync.

## D-31 — GCP deployment with Terraform
**Superseded by D-46.**
**Origin:** Brief (stack) + Proposed (details)
**Decision:**
- Terraform manages Artifact Registry, Cloud Run (API service + worker), Cloud SQL for PostgreSQL with the `vector` extension, Secret Manager, least-privilege service accounts, and Workload Identity Federation for GitHub Actions CD (no service-account keys).
- The worker runs as a Cloud Run **worker pool** if it is available in the region at deploy time; otherwise as a Cloud Run service with min-instances=1 and instance-based billing.
- Migrations run as a Cloud Run job before the traffic switch.
- The Terraform GitHub provider creates the `cicd-doctor-lab` repository from `lab/`.

**Why:** A managed and cheap setup for a low-traffic app. The worker needs continuous CPU to poll the queue (D-05), which a request-driven service doesn't give by default. WIF avoids long-lived credentials in GitHub.
**Alternatives:** GKE (overkill; Kubernetes stays a local extra, D-37); Cloud Tasks to trigger the worker (see D-05).
**Consequences:** An always-on worker plus Cloud SQL is the main fixed cost; use the smallest tiers and document the expected monthly cost in the README.

## D-32 — Observability: structlog JSON + Langfuse
**Origin:** Brief (tools) + Proposed (fields)
**Decision:** JSON logs via structlog with `delivery_id`, `job_id`, `run_id`, `run_attempt`, `installation_id`, `repository` and `event` on every line, collected by Cloud Logging, plus a redaction processor. Langfuse (P5) traces every LLM call with prompt version, model, tokens, cost and latency, and only redacted text. Tracing is off when `LANGFUSE_*` is unset.
**Why:** One diagnosis can be followed end to end by its correlation ids. Langfuse gives prompt-level visibility and cost tracking that match the eval metrics.
**Alternatives:** OpenTelemetry tracing everywhere (more setup; can be added later); only logs (no LLM-level view).
**Consequences:** Langfuse is another external service receiving redacted excerpts. It can be self-hosted if needed.

## D-33 — Evals: committed fixtures, cached LLM responses, LLM judge, CI gate
**Origin:** Brief (datasets and metrics) + Proposed (mechanics)
**Decision:**
- Cases are versioned in `evals/cases/` (lab, public with fix commits, injection), with redacted logs and labels.
- LLM responses are cached by `(model, prompt_version, case_hash)`, so the CI evals are offline, deterministic and free, and `--live` refreshes them.
- Cause match is scored by a separate LLM judge on a 0/1/2 scale, calibrated against 20 manual labels.
- The variants `rules`, `llm`, `llm+hints` and `llm+hints+similar` are compared.
- CI fails on a macro-F1 drop of more than 5 points or any injection success.

**Why:** Reproducibility is what makes the comparison credible, and the cache keeps CI cheap. A calibrated judge is the practical way to compare free-text causes with real fixes.
**Alternatives:** Live LLM calls in CI (costly, flaky); exact keyword matching for causes (too brittle).
**Consequences:** Public log excerpts are stored in the repo, so they are limited to excerpts and short diff summaries, with licenses respected.

## D-34 — Data retention: 30 days deliveries, 90 days failures
**Origin:** Proposed
**Decision:** A daily purge job deletes `webhook_deliveries` older than 30 days, and failures (with their diagnoses) older than 90 days. Uninstalled installations are soft-deleted and their data is purged on the same schedule.
**Why:** 90 days matches GitHub's default log retention, so the memory doesn't outlive the source. 30 days is far more than any redelivery window.
**Alternatives:** Keeping everything (privacy and storage cost); per-installation retention settings (unnecessary for v1).
**Consequences:** Similarity search only covers the last 90 days. Widened and scheduled by D-85.

## D-35 — Store both the rules and the LLM diagnosis for every failure
**Origin:** Proposed
**Decision:** Every failure gets a `rules` diagnosis row, and an `llm` or `cache` row when applicable. `published` marks the one shown to users.
**Why:** Production data becomes an ongoing rules-vs-LLM comparison, and agreement rates can be monitored without rerunning anything.
**Alternatives:** Storing only the published diagnosis.
**Consequences:** Roughly twice the rows in `diagnoses`; negligible at this scale.

## D-36 — English for code, commits, README and design docs
**Origin:** Brief (code, commits, README) + Interview (SPEC and DECISIONS)
**Decision:** All repository content is written in English, including SPEC.md and DECISIONS.md.
**Why:** It is consistent, and readable for anyone reviewing the portfolio.
**Consequences:** None.

## D-37 — Local Kubernetes is an optional extra
**Origin:** Brief
**Decision:** kind manifests (or a Helm chart) for api + worker + Postgres come in P6 as an optional extra. Production stays on Cloud Run.
**Why:** It shows Kubernetes skills without paying for or operating a cluster that the workload doesn't need.
**Consequences:** The manifests must reuse the same image and environment contract as Cloud Run.

## D-38 — Local Postgres is published on host port 5433
**Origin:** Proposed
**Decision:** The Compose `db` service listens on 5432 inside the container and is published on `127.0.0.1:${POSTGRES_PORT:-5433}`. `.env.example` sets `POSTGRES_PORT=5433` and a `DATABASE_URL` that points to it. Setting `POSTGRES_PORT=5432` in `.env` restores the conventional port.
**Why:** Running `docker compose up` on the author's Windows machine showed a native PostgreSQL already bound to `0.0.0.0:5432`. Native installs are common on developer machines, and stopping someone's own database service just to start a project is the wrong default. The port is bound to loopback so the development database is never exposed to the network.
**Alternatives:** Keep 5432 and ask the developer to stop the native service (invasive, and it fails again on the next machine with the same setup); publish no host port (the API and worker run on the host with `uv run` until P5, so they could not connect); a random port (the connection URL would change on every start).
**Consequences:** SPEC §4.6, `.env.example` and the README use 5433. The CI service container and Cloud SQL are not affected, because they do not share the developer's port space.
**Amended by D-52:** `DATABASE_URL` in `.env.example` uses `127.0.0.1` instead of `localhost`.

## D-39 — `api/` and `worker/` are sibling packages; `app/` is only the composition root
**Origin:** P0 request
**Decision:** `src/cicd_doctor/` contains `core/`, `adapters/`, `api/` (the FastAPI app, in `api/main.py`), `worker/` (the job consumer) and `app/` (settings, the wiring of adapters into use cases, and the `cicd-doctor` command in `app/cli.py`). This replaces the placement of `api.py` and `worker.py` inside `app/` that SPEC §4.2 had at first. `core/` must not import any of these packages (D-43).
**Why:** The API and the worker are inbound adapters in the hexagon (SPEC §4.1), so they sit beside `adapters/` instead of inside the composition root. `app/` keeps a single job: building the object graph. The author asked for these folders in the Phase 0 request.
**Alternatives:** The original layout (`app/api.py`, `app/worker.py`), which mixes entry points with wiring.
**Consequences:** uvicorn targets `cicd_doctor.api.main:app`. `app/settings.py` and `app/container.py` arrive in P1. D-02 still holds: `app/` is the composition root.
**Amended by D-55:** uvicorn now starts the factory `cicd_doctor.app.container:create_api`.

## D-40 — The liveness endpoint is `GET /health`
**Origin:** P0 request
**Decision:** The API answers `GET /health` with `{"status": "ok"}` while the process is serving requests. This replaces the `/healthz` name in SPEC §4.4. The readiness endpoint (the database is reachable) stays planned for P1.
**Why:** The author asked for `/health` in the Phase 0 request, and one plain name is enough for a liveness check.
**Alternatives:** `/healthz` (a common convention, but nothing in this project depends on it); serving both names (two URLs for one check).
**Consequences:** Health probes in P5 (Compose, Cloud Run) must use `/health`. The readiness endpoint, still called `/readyz` in the spec, is named when it is built in P1 so that the pair stays consistent.
**Amended by D-53:** the readiness endpoint is `GET /ready`.

## D-41 — Compose lives in `docker/compose.yaml`, included by a root `docker-compose.yml`
**Origin:** P0 request
**Decision:** The Compose definition is `docker/compose.yaml`, the path used by SPEC §4.6 and the P0 acceptance criteria. A root `docker-compose.yml` contains only `include: [docker/compose.yaml]`, so `docker compose up -d db` works from the repository root and reads `.env`. Both files are valid entry points. The definition sets `name: cicd-doctor`, so the project, network and volume names are the same from either one. `docker/initdb/01-extensions.sql` runs `CREATE EXTENSION IF NOT EXISTS vector` on first start. Postgres is the only service, because it is also the queue (D-05).
**Why:** The author asked for a `docker-compose.yml`, and the spec fixed `docker/compose.yaml`. With `-f docker/compose.yaml` Compose takes `docker/` as the project directory, which would skip the root `.env` and name the project `docker`. The shim and the explicit `name` remove both problems.
**Alternatives:** Only a root file (breaks the spec's path, and the P5 Dockerfile is meant to live in `docker/`); only `docker/compose.yaml` (does not give the author the root file they asked for).
**Consequences:** Two files to know about; `include` needs Compose 2.20 or later. Every `${VAR}` in the definition keeps a default, so `-f docker/compose.yaml` also works without a `.env`. Alembic still owns the schema (SPEC §6); the init script only makes pgvector available.

## D-42 — Pre-commit runs ruff and the fast tests; mypy runs in CI
**Origin:** P0 request
**Decision:** `.pre-commit-config.yaml` runs `ruff check --fix`, `ruff format` and `pytest -m "unit or contract" -x -q`, as local hooks through `uv run`, so the tool versions come from `uv.lock`. `mypy --strict` runs in the `types` job of CI and on demand with `uv run mypy`. This amends D-29, which also listed mypy and import-linter in pre-commit.
**Why:** The author asked for ruff and the fast tests in the commit gate. The gate takes about a second and still rejects broken commits (checked with a deliberately failing test). Type errors still block merges through CI.
**Alternatives:** The D-29 set (mypy and import-linter in pre-commit too), which catches type errors before the push at the cost of a slower commit.
**Consequences:** A type error can be committed locally and is caught by CI. The Claude Code `PostToolUse` hook has the same scope (ruff and fast tests). Revisit adding mypy to pre-commit if type errors keep reaching CI.

## D-43 — The `core/` import rule is enforced by a pytest test, not import-linter
**Origin:** P0 request
**Decision:** `tests/unit/test_architecture.py` parses every module under `core/` with `ast` and fails if it imports `cicd_doctor.adapters`, `.app`, `.api` or `.worker`, or one of `fastapi`, `starlette`, `uvicorn`, `httpx`, `sqlalchemy`, `psycopg`, `openai`, `fastembed`. Relative imports and `from cicd_doctor import adapters` are resolved. Pydantic stays allowed (D-03). import-linter is not used. This amends D-02, D-29 and NFR-09.
**Why:** The author asked for a test. It runs inside the fast suite, so pre-commit, the Claude Code hook and CI enforce the rule without another tool or configuration, and the forbidden list is the one D-02 defined. The test is itself tested with synthetic modules, and was checked once by adding `from cicd_doctor.adapters.memory import FakeEmbedder` to `core/` (it failed) and reverting.
**Alternatives:** import-linter, which expresses the contract declaratively and can follow indirect import chains.
**Consequences:** Only direct, static imports inside `core/` are checked: dynamic imports (`importlib`) and chains through other first-party packages are not followed. Adding a new forbidden library means editing the list in the test. Revisit import-linter if that gap ever matters.
**Amended by D-50:** the forbidden list also contains `alembic` and `pydantic_settings`.

## D-44 — `httpx2` backs the FastAPI test client
**Origin:** Proposed
**Decision:** The dev dependency group contains `httpx2` instead of `httpx` for `fastapi.testclient.TestClient`. This amends D-28 for tests only. **Amended by D-67:** `httpx2` is also the GitHub client, so it is now a runtime dependency.
**Why:** The installed Starlette prefers `httpx2` and emits a deprecation warning when it falls back to `httpx`.
**Alternatives:** Keep `httpx` and tolerate the warning, which is likely to become an error in a future Starlette release.
**Consequences:** When P1 adds `httpx` as the runtime client for the GitHub adapter (D-28), check that `respx` still mocks it as expected, since the test client no longer shares that package. **Resolved by D-67:** the GitHub adapter uses `httpx2` too, so there is one HTTP stack and `respx` is not needed. `httpx2` is now a runtime dependency, not a dev one.

## D-45 — CI runners are pinned to `ubuntu-24.04` instead of `ubuntu-latest`
**Origin:** Author request
**Decision:** Every job in `.github/workflows/ci.yml` uses `runs-on: ubuntu-24.04`. The workflows added later (`evals.yml` in P4, `cd.yml` in P5) follow the same rule, and the runner image only changes through a commit.
**Why:** The annotations on the first CI run warned that the `ubuntu-latest` label will migrate to Ubuntu 26 beginning October 19, 2026. With the floating label, the image and its preinstalled tools would change without any commit in this repository, so a green pipeline could turn red, or behave differently, with nothing in the history to bisect. A fixed image keeps CI reproducible, in line with the rest of the pipeline, which is already pinned (actions by commit SHA, dependencies by `uv.lock`).
**Alternatives:** Keep `ubuntu-latest` (no maintenance, but the image changes on GitHub's schedule and not on ours); move straight to the Ubuntu 26 image (also a deliberate choice, but there is no reason to take that jump now, and it can be one reviewed commit later).
**Consequences:** The pinned image will eventually leave standard support and has to be bumped by hand: one line per job, three today. Do it in a commit that also shows CI green on the new image. The bump is low risk because Python and `uv` come from `astral-sh/setup-uv` and not from the runner image.

## D-46 — AWS deployment with Terraform (replaces GCP)
**Origin:** Author request
**Decision:**
- Production runs on AWS, managed with Terraform: ECR for images, an ECS cluster on Fargate with two services built from the same image (the API behind an Application Load Balancer, and the worker with desired count 1), RDS for PostgreSQL 17 with the `vector` extension, AWS Secrets Manager for secrets, least-privilege IAM task and execution roles, and CloudWatch Logs for the JSON logs.
- GitHub Actions CD authenticates with GitHub OIDC and an IAM deploy role (no long-lived access keys). It builds and pushes to ECR, runs the migrations as a one-off ECS task, and only then updates the services.
- The Terraform GitHub provider still creates the `cicd-doctor-lab` repository from `lab/`.
- The prod GitHub App webhook points at the API's public HTTPS endpoint.
- The Terraform rule family of the diagnoser (D-13) targets AWS: `AWS-ACCESS-DENIED`, `AWS-CREDENTIALS`, `AWS-OIDC` and `AWS-CONFLICT` replace the four `GCP-*` rules. The lab case "GCP API disabled" becomes "AWS `AccessDenied`". The redaction patterns for GCP credentials stay, because the redactor is provider-agnostic.
- Open for P5 (SPEC §14): the HTTPS entry point (ALB with an ACM certificate, which needs a domain, or App Runner for the API) and Secrets Manager vs SSM Parameter Store.

**Why:** The author wants to build and show the deployment on AWS. Nothing before P5 depends on the cloud provider, because `core/` has no infrastructure imports (D-43) and the queue is Postgres (D-05). The worker needs continuous CPU to poll the queue, which a long-running Fargate service gives directly, and one image keeps the API and the worker in the same environment contract. Rules for the provider the project itself deploys to let CICD Doctor diagnose its own CD (D-13).
**Alternatives:** Keep GCP (D-31); Azure (equivalent services, but not the author's target); App Runner for the API and Fargate for the worker only (simpler HTTPS, but two compute products); Lambda behind a function URL plus SQS (event-driven fit, but needs a second queue adapter and behaves differently in dev and prod, as in D-05); EKS (overkill, Kubernetes stays a local extra, D-37).
**Consequences:** Supersedes D-31 and amends D-13. Wherever earlier entries or the code comments say Cloud Run, Cloud SQL, Secret Manager, Cloud Logging or Cloud Tasks, read ECS Fargate, RDS, Secrets Manager, CloudWatch Logs or SQS (D-05, D-07, D-30, D-32, D-37, D-38, D-40). Health probes in P5 use `/health` (D-40) as the ALB target group check. The ALB and RDS are the main fixed costs; use the smallest tiers (for example `db.t4g.micro`), avoid a NAT gateway by running tasks in public subnets restricted by security groups, and document the expected monthly cost in the README. SPEC §13 P5 and §10 are updated in the same change.

## D-47 — Job leases are fenced by attempt; `heartbeat` and `wait_for_jobs` join the `JobQueue` port
**Origin:** Proposed
**Decision:**
- Every `lease` increments `attempts`, so `(id, attempts)` identifies one lease. `complete`, `retry` and `fail` take the leased `Job` instead of a `job_id`, and return `bool`: `False` means the lease is no longer the current one and nothing changed. Each of these statements ends in `WHERE id = :id AND attempts = :attempts AND status = 'running'`.
- `heartbeat(job, lease) -> bool` moves `locked_until` to `now + lease`. The worker calls it every `lease / 3` while the handler runs, and cancels the handler when it returns `False`.
- `wait_for_jobs(max_wait) -> bool` returns when new jobs may be ready, or after `max_wait`. The Postgres adapter implements it with `LISTEN jobs_new` on a dedicated autocommit connection that reconnects with backoff (1 s to 30 s); the in-memory one with an event set by `enqueue`. The worker still polls every 5 s.
- `InMemoryJobQueue` implements the same contract, and one contract suite runs against both adapters (SPEC §12).

**Why:** Without a fence, a worker whose lease expired could finish late and mark as done (or queue again) a job that another worker is running; a late `retry` would even let a third worker lease it. Without a heartbeat, every job longer than `JOB_LEASE_S` runs twice at the same time, which from P1 means two LLM calls and twice the budget. `wait_for_jobs` keeps the worker, an inbound adapter, free of Postgres details: it only sees the port.
**Alternatives:** The `job_id` signatures first written in SPEC §4.3 (no protection against late outcomes); fencing by `locked_by` (a worker that leases its own expired job again would pass the check; `attempts` only grows); a longer lease instead of heartbeats (slower recovery after a crash); a separate notifier port.
**Consequences:** SPEC §4.3 and §6 describe the new methods and queries. Amends D-05. A notification that arrives while no consumer is waiting is dropped, so the worst case is one poll interval of extra latency.

## D-48 — The queue takes time from the injected `Clock`, not from SQL `now()`
**Origin:** Proposed
**Decision:** `PostgresJobQueue` reads the time from the `Clock` port and passes it as `:now` to every statement: `run_after`, `locked_until`, the lease comparisons, `updated_at` and `received_at`. The `DEFAULT now()` of the columns stays for rows written by hand. This replaces the `now()` calls in the SPEC §6 lease query.
**Why:** The same contract suite and the "transient error → increasing `run_after` → `dead`" acceptance test (SPEC §13 P1a) then run deterministically on both adapters with a `FakeClock`, without sleeping and without editing rows behind the adapter's back.
**Alternatives:** SQL `now()` as first written (the database is the only time source, but the Postgres adapter can then only be tested in real time); a `FakeClock` for the in-memory adapter only (the Postgres semantics would go untested).
**Consequences:** Lease decisions compare times taken on different hosts, so clock skew between workers shifts lease expiry by that skew. Production runs one worker task (D-46) and Fargate keeps time with NTP, which is far below the 300 s lease. Revisit (for example `clock_timestamp()` in SQL behind a flag) if several worker hosts run with poor time sync.

## D-49 — Worker execution policy: outcomes, unexpected errors, backoff, shutdown
**Origin:** Proposed
**Decision:**
- `cicd-doctor worker` runs `WORKER_CONCURRENCY` consumers. Each one leases a job, runs the handler registered for its kind, records the outcome, and when idle waits for `wait_for_jobs` or 5 s.
- Outcomes: success → `done`. `TransientError` → `retry` at `now + backoff`, or `dead` when `attempts == max_attempts`. `PermanentError` → `failed`. A kind without a handler → `failed`. **Any other exception is treated as transient** and logged with its traceback. `TransientError.retry_after` (from `Retry-After` or `x-ratelimit-reset`) replaces the computed backoff.
- The backoff is a pure function in `core/retry.py`: the ceiling is 30 s × 4^(n−1), capped at 30 min; the delay is that ceiling times a jitter in [0, 1] drawn by the caller (full jitter). Tests pin the jitter to 1.
- A consumer survives queue errors (the database is down, for example): it logs them and tries again after a poll interval. The Postgres adapter turns connection-level SQLAlchemy errors into `TransientError`.
- Shutdown: SIGTERM and SIGINT on POSIX set a stop event; on Windows, Ctrl+C cancels the main task. Both stop the leasing, give running jobs 30 s to finish, and then cancel them; a cancelled job stays `running` and is leased again when its lease expires.
- `last_error` is `"<ExceptionType>: <message>"`, cut to 2 KB on a character boundary by the queue. It goes through `core/redaction.py` once that exists (P1b). No handler logs job payloads.
- Until each use case exists, every job kind has a placeholder handler that logs the job and completes it. Nothing enqueues jobs before the webhook (P1c).
- Logs use the standard `logging` module in plain text until structlog arrives (D-32).

**Why:** Retrying unknown errors means that an error a library raises unwrapped (a dropped database connection, for example) never loses work, while a real bug still ends up `dead`, logged at ERROR, after about 40 minutes. Keeping the backoff pure and the jitter injected makes the retry schedule exactly testable. Draining on shutdown avoids wasting an attempt on every deploy.
**Alternatives:** Treating unknown errors as permanent (an unwrapped transient error would lose the job); equal jitter (the shortest delay never goes near zero, but the spec asks for full jitter); per-kind handler modules registered by import side effects (implicit wiring).
**Consequences:** A deterministic bug costs five attempts before it is buried. With full jitter a retry can happen almost at once, which is acceptable for the errors involved. Amends D-23.

## D-50 — Migrations are hand-written SQL; Alembic lives in the Postgres adapter
**Origin:** Proposed
**Decision:** `alembic.ini` is at the repository root and the scripts are in `src/cicd_doctor/adapters/postgres/migrations/`. Each revision is a tuple of SQL statements, each run with `op.execute`. There is no SQLAlchemy table metadata and no autogenerate. `env.py` runs on a sync psycopg engine with a 10 s connect timeout. It takes the URL from `config.attributes["database_url"]` when a caller passes one (the tests), otherwise from the settings (`DATABASE_URL`, `.env`). The downgrade drops every table and type but keeps the `vector` extension, which the Compose init script also creates. Revision ids are short sequence numbers (`0001`). The `core/` import test also forbids `alembic` and `pydantic_settings`.
**Why:** SPEC §6 is written in SQL, so copying it statement by statement makes the migration reviewable as a diff against the spec, and the queue adapter uses explicit SQL as well (D-28). Autogenerate would need a second definition of the schema (table metadata, plus pgvector types) that nothing else uses.
**Alternatives:** `op.create_table` with SQLAlchemy column types (more verbose and harder to compare with §6); metadata with autogenerate (convenient for later changes, but two schema definitions to keep in sync).
**Consequences:** Every schema change is written by hand, together with its check in `tests/integration/test_migrations.py`. Amends D-43 (the forbidden list).

## D-51 — Postgres tests: own databases, "integration wins", skip locally, fail in CI
**Origin:** Proposed
**Decision:**
- Tests that need Postgres use the database `<db>_test`, and the migration round trip uses `<db>_test_migrations`. Both are created on first use. The URL is `TEST_DATABASE_URL`, or else `DATABASE_URL` with `_test` appended to the database name.
- Contract suites run against `memory` and `postgres`. The `postgres` parameter is marked `integration`, and `tests/conftest.py` does not add the `unit` or `contract` directory marker to a test already marked `integration`. So pre-commit, the Claude Code hook and `-m "unit or contract"` never need a database.
- When Postgres is unreachable, those tests are skipped locally with the command that starts it. When `CI` is set (GitHub Actions sets it), they fail instead.
- The CI `tests` job runs `pgvector/pgvector:pg17` as a service container, runs `uv run alembic upgrade head` through the real CLI on the empty database, and then the whole suite.

**Why:** The developer's own data is never truncated, the commit gate stays fast and database-free, and `uv run pytest` still works without Docker. But CI cannot go green while silently skipping every integration test.
**Alternatives:** testcontainers (starts Docker from Python, heavier on Windows); always skipping when Postgres is unreachable (CI could pass with no integration test run); reusing the development database (the tests truncate the queue tables).
**Consequences:** `tests/pg.py` and `tests/conftest.py` own the setup. The crash test starts a real Python subprocess and kills it.

## D-52 — Windows: selector event loop for psycopg async, and `127.0.0.1` instead of `localhost`
**Origin:** Proposed
**Decision:** The worker runs `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)`, and the tests get the same loop through pytest-asyncio's `pytest_asyncio_loop_factories` hook. `DATABASE_URL` in `.env.example` uses `127.0.0.1`, and every connection has a connect timeout (10 s in the app, 5 s in the tests). This amends D-38, whose URL used `localhost`.
**Why:** psycopg 3.3 refuses the Proactor event loop that Windows uses by default (checked: `InterfaceError`). On the author's machine `localhost` resolved to `::1` first, and the connection to Docker's port 5433 hung there: 5 s with a timeout, forever without one. `127.0.0.1` connected in about 30 ms, and Compose publishes the port on `127.0.0.1` only (D-38).
**Alternatives:** Setting `WindowsSelectorEventLoopPolicy` globally (deprecated since Python 3.14); asyncpg (no such issue, but D-28 chose psycopg and Alembic would need a second driver); keeping `localhost` and relying on the timeout (a 5 s stall on every connection).
**Consequences:** asyncio subprocesses are not available on Windows under the selector loop, so the crash test uses `subprocess.Popen`. A developer whose `.env` was copied before this change must switch `localhost` to `127.0.0.1` there too.
**Amended by D-55:** the API runs on the selector loop as well, because uvicorn picks Proactor on Windows unless it reloads.

## D-53 — The readiness endpoint is `GET /ready`
**Origin:** Author request (chosen when the endpoint was built, as D-40 planned)
**Decision:** The API answers `GET /ready` with `{"status": "ready"}` (200) when the database answers `SELECT 1` within 2 s, and `{"status": "unavailable"}` (503) otherwise. This replaces the `/readyz` name in SPEC §4.4. `PostgresJobQueue.ping()` does the check; it is not part of the `JobQueue` port.
**Why:** `/health` and `/ready` form a consistent pair without the Kubernetes-style `z`, which D-40 asked for. Liveness stays free of dependencies, so a database outage makes the API not ready instead of dead.
**Alternatives:** `/readyz` (the name first written in the spec; mismatched with `/health`); folding the database check into `/health` (a restart loop when the database is down).
**Consequences:** The P5 ALB health check uses `/health`; a readiness probe, where one exists, uses `/ready`. Amends D-40.

## D-54 — Installation events are applied by the worker through a `sync_installation` job
**Origin:** Author request (the worker instead of the request path) + Proposed (the details)
**Decision:**
- The webhook turns `installation` (`created`, `deleted`, `suspend`, `unsuspend`) and `installation_repositories` (`added`, `removed`) into a `sync_installation` job, enqueued with the delivery in one transaction, like every other job. `new_permissions_accepted` and other actions are ignored (204).
- The job payload is an `InstallationEvent`: the action, the installation's id and account, the repositories added (or listed by `created`), the ids removed, and the time the webhook was received.
- A new `InstallationStore` port (upserts of installations and repositories, plus reads) has in-memory and Postgres adapters sharing one contract suite. The use case `core/usecases/track_installation.py` sets `deleted_at`, `suspended_at` and `removed_at` from the event time and clears them again on `created`, `unsuspend` or a repository added back.
- Migration `0002` makes `repositories.default_branch` nullable: installation events list repositories without it. A save without a default branch keeps the stored one; P1c fills it in from the `workflow_run` payload.

**Why:** It keeps the request path to verify, record and enqueue (SPEC §7.1, D-04), the delivery dedupe protects these events too, and a database hiccup while applying them is retried like any job.
**Alternatives:** Applying the upserts in the webhook transaction (no job, but more work in the request path); fetching the default branch from the GitHub API (a GitHub call per repository, with a token, for a value the first `workflow_run` brings for free).
**Consequences:** Two events of the same installation handled at the same time by different consumers can be applied out of order, leaving a stale state until the next event. These events are rare, and P1c refreshes the repository from each `workflow_run`. An installation installed before this code ran has no row until its next installation event or its first diagnosed run (P1c upserts it).

## D-55 — The API receives its dependencies; uvicorn starts it from the composition root
**Origin:** Proposed
**Decision:** `api/main.py` exposes `create_app(dependencies)`, where `dependencies` is either a ready `ApiDependencies` (tests: in-memory queue, test secret) or a provider opened in the FastAPI lifespan (production). `ApiDependencies` holds the queue, the webhook secrets (current, then previous), the readiness check, the clock, `max_attempts` and the enqueue timeout. `app/container.py` builds the real one from the settings in `create_api()`, and `cicd-doctor api` runs `uvicorn.run("cicd_doctor.app.container:create_api", factory=True, loop="asyncio:SelectorEventLoop")`. There is no module-level `app` any more.
**Why:** The inbound adapter no longer builds infrastructure, so the webhook can be tested with in-memory adapters and the composition root stays the only place that knows Postgres (D-39). uvicorn 0.54 picks the Proactor loop on Windows unless it reloads, which psycopg refuses (D-52); its `loop` option accepts an import string.
**Alternatives:** A module-level `app` that reads the settings at import time (importing the module would need a `.env`); a FastAPI `Depends` per request (the queue and its engine must live for the whole process).
**Consequences:** `TestClient(create_app(...))` needs no lifespan for in-memory dependencies. Amends D-39 (the uvicorn target) and D-52 (the API runs on the selector loop too).

## D-56 — Webhook request policy: check order, what is stored, timeouts, normalised payloads
**Origin:** Proposed
**Decision:**
- Order: size (413 past 25 MB, by `Content-Length` and while streaming) → signature (401; 503 if no secret is configured) → `X-GitHub-Event`, `X-GitHub-Delivery` (a UUID) and a JSON object (400) → `ping` (200) → routing (204 when no job applies; 400 when a handled event lacks a field) → enqueue (202, 200 for a duplicate delivery or dedupe key, 503 when the database fails or does not answer within 5 s).
- Only deliveries that create a job are recorded in `webhook_deliveries`. `ping` and unhandled events store nothing.
- The signature is compared against every configured secret without stopping at the first match. Nothing from an unverified request is logged; accepted requests log the delivery id, event, action and outcome, never the payload.
- Job payloads are normalised (`DiagnoseRunPayload`, `InstallationEvent`), never the raw GitHub JSON, which carries commit messages, author e-mails and other data the pipeline does not need. Error messages for invalid payloads name fields, never values.
- The webhook fixtures in `tests/fixtures/webhooks/` are synthetic payloads shaped like GitHub's documented ones, with invented names and e-mails, not recordings of real deliveries.

**Why:** Each step costs more than the one before it, so cheap rejections come first and nothing is written for an unauthenticated or useless request. The 5 s bound keeps the answer under GitHub's 10 s timeout (NFR-01): if the commit went through anyway, a redelivery is a harmless duplicate (D-06). Storing less personal data in the queue narrows what a database leak exposes.
**Alternatives:** Recording every verified delivery, including ignored ones (a fuller audit trail, but rows for events nobody acts on); storing the raw payload in the job (simpler now, but personal data in every row and a looser contract for the handlers); committing recorded real payloads (more realistic, but real names and e-mails in the repository).
**Consequences:** The P1c diagnose handler reads `DiagnoseRunPayload` and must fetch anything else it needs from GitHub. An ignored delivery that is redelivered is ignored again, which is the intended outcome.

## D-57 — Token counting goes through a `TokenCounter` port; tiktoken lives in an adapter; the budget is water-filled
**Origin:** Proposed
**Decision:**
- `core/logs` counts tokens through a new port, `TokenCounter` (`count(text) -> int`). `adapters/llm/tiktoken_counter.py` implements it with tiktoken `o200k_base` (`encode_ordinary`, so special-token text in a log is plain text), loaded on first use. `adapters/memory/tokens.py` has `CharTokenCounter` (about 4 characters per token) for the offline tests.
- An excerpt line costs the tokens of `L<number>: <text>\n`, the form the prompt shows it in (P1b, LLM part). `token_count` is the sum over the kept lines.
- When the failed jobs of a run need more than `EXCERPT_MAX_TOKENS` together, the budget is split evenly and what a job does not use is shared again between the others (water-filling).
- Lines longer than 2,000 characters are cut after redaction, with a `…[line truncated]` suffix.

**Why:** tiktoken downloads its encoding file the first time it is used. That is network IO, which `core/` must not do (D-02), and it would make the offline fast suite depend on the network. A plain equal split would waste the share of a short excerpt while another job is cut.
**Alternatives:** Importing tiktoken in `core/` (IO on a cold cache); vendoring the encoding file (several MB in the repository); estimating tokens from characters everywhere (the 6,000-token limit would only be approximate).
**Consequences:** Tests that need real counts, such as the `token_count ≤ 6000` criterion, live in `tests/adapters/`, outside the fast suite, and need the network once on a cold cache. CI runs them. The diagnose use case (P1c) wires `TiktokenCounter`.

## D-58 — `core` selects the failed jobs and step; line numbers count in the retained log
**Origin:** Author request (selection in `core`, approved with the P1b plan) + Proposed (the details)
**Decision:**
- `LogSource.failed_jobs(run) -> list[FailedJob]` becomes `LogSource.jobs(run) -> list[WorkflowJob]`. The adapter returns every job of the attempt, normalised (`WorkflowJob`, `WorkflowStep`), and `core/logs/select.py` keeps the `failure` and `timed_out` jobs, earliest `completed_at` first (ties by job id), at most `LOG_MAX_JOBS`. The failed step is the first step with conclusion `failure`; when there is none (a timed-out job), the first `timed_out` or `cancelled` step; else the whole job log is used.
- `FailedJob` gains `conclusion` (`failure` or `timed_out`, needed by GHA-TIMEOUT) and `step_started_at` / `step_completed_at` (needed to slice the step out of the log).
- `adapters/github/jobs.py` parses the jobs API JSON, without HTTP. The P1c client and the `jobs.json` of the eval cases both use it. `runner_os` comes from the runner labels.
- Line numbers are 1-based in the retained log. When the log was cut to its tail (`truncated = true`), they count from the first complete line of the tail, because the number of dropped lines is unknown. Renderers must not link those numbers to a line of GitHub's log view.
- The retained tail starts at a complete line: the partial first line is dropped, unless the cut falls right after a newline.
- `omitted` lists the ranges dropped before the first kept line and between kept lines, not after the last one (the excerpt keeps the end of the step).

**Why:** Which jobs and which step to read is domain logic with edge cases (timed-out jobs have no failed step, ties, unfinished jobs) that belongs in unit-tested `core` code rather than in the HTTP adapter. The step times must travel with the job to slice the log.
**Alternatives:** Selecting in the adapter (the port as first written); counting the lines of the dropped head to keep absolute line numbers (it means reading the whole log, which the `Range` request avoids).
**Consequences:** A port changes before any adapter implements it, so nothing breaks. Evidence in a truncated excerpt links to the job, not to the line.

## D-59 — Redaction and signature details: order, thresholds, PEM spans, generated corpus, fixed-point normalisation
**Origin:** Proposed
**Decision:**
- Redaction order: structural patterns first (inline PEM, URL credentials, `Authorization` headers, `aws_secret_access_key`), then token shapes, then generic `key=value` assignments. Placeholders are never redacted again, so redaction is idempotent and a second pass counts nothing.
- A value assigned to a secret-named key (`token=`, `"api_key": "…"`) is redacted whatever its shape, UUIDs and hex strings included (Codecov upload tokens are UUIDs). SHAs and UUIDs anywhere else are never touched.
- Generic values are redacted when they have 20 or more characters, or an entropy of at least `min(3.5, log2(length) − 1)` bits per character. §9.2's fixed 3.5 bits cannot be reached by a value shorter than 12 characters (n characters carry at most log2 n bits each), so short passwords never matched. Variable references (`$X`, `${X}`, `%X%`), paths and values containing GitHub's `***` mask are skipped.
- Multi-line PEM blocks are located over all the lines of the step, so a window that starts in the middle of a key still redacts it. A block without its `END` line runs while the lines look like PEM content (base64, headers, blank lines). Each redacted line of a block counts as one replacement.
- In the excerpt, redaction runs on the windowed lines before tokens are counted and long lines are cut, not on every line of a 20 MB step. Nothing unredacted leaves `build_excerpts`.
- The worker redacts `jobs.last_error` (completes D-49).
- The redaction corpus (`tests/redaction_corpus.py`) assembles its fake secrets at import time from fragments and a seeded random generator. No secret-shaped literal is committed, so GitHub's secret scanning and push protection have nothing to flag.
- Signature: `normalize` applies its rules until nothing changes, which makes it idempotent by construction (one pass can uncover a new match, such as two workspace paths glued together). Workspace paths become repository-relative with `/` separators, so Linux, macOS and Windows runs of one error share a hash. `[<ipv6>]:<port>` becomes `[<ip>]:<port>`. Digits glued to letters (`x64`, `py3`) are kept. When an excerpt has no error line, its last five non-blank lines stand in for the error lines in the canonical text.

**Why:** The corpus found a short planted password that the fixed threshold let through, and the property tests found two glued workspace paths that made `normalize` change its own output. The other points close gaps seen while writing the tests: a placeholder that a later rule would redact again, a UUID-shaped token, and Windows paths that would give one error two signatures.
**Alternatives:** Keeping §9.2's fixed entropy threshold (short passwords leak); exempting UUIDs and hex everywhere (leaks UUID-shaped tokens); literal fake secrets in a fixture file (push protection may block the push, and scanners raise alerts).
**Consequences:** More false positives after secret-named keys (a nine-character dictionary word with a digit after `password:` is redacted, and so is a code line such as `token = get_token()` quoted in a traceback), which D-12 accepts. The corpus is less readable as data, but each case states its kind, its line and the value that must disappear.

## D-60 — Rule engine: per-rule priorities, context conditions, one match per rule, `core/analysis.py`
**Origin:** Proposed
**Decision:**
- Each rule has its own priority. 3 is decisive: the run's own verdict or a cause nothing else explains (GHA-TIMEOUT, GHA-RUNNER-LOST, GHA-INPUT-REQUIRED, PY-NO-DIST, DOCKER-HUB-LIMIT, TF-LOCK, AWS-OIDC). 2 is a specific cause (most rules). 1 is a symptom or a fallback (PY-PYTEST-FAIL, GHA-CONN, GHA-CANCEL-TIMEOUT, DOCKER-RUN-FAIL). Matches are ranked by priority, then confidence, then earliest evidence line.
- A rule fires once per excerpt. Its evidence is every matching line (at most 8). Its cause and fix templates take the first value of each named group over all matching lines, cut to 120 characters; a placeholder without a value uses the rule's default or `?`. Captured values are never re-read as templates.
- Context conditions:
  - `on_conclusion`: GHA-TIMEOUT also fires for a `timed_out` job with no matching line.
  - `tail_patterns`: GHA-OOM's `Killed` only counts in the last 20 lines.
  - `when`: PY-RUFF needs `ruff` in the step name or the excerpt.
  - `refine`: PY-PYTEST-COLLECT becomes `dependency` when a `ModuleNotFoundError` is in the excerpt.
- Patterns added to the §10 table:
  - GHA-CANCEL-TIMEOUT: `has timed out after N minutes`.
  - GHA-DNS: `Name or service not known`, `nodename nor servname provided`.
  - GHA-CONN: `ECONNREFUSED`.
  - PY-NO-DIST: uv's `Because X was not found in the package registry`.
  - PY-MYPY: its `[code]` pattern is anchored at the end of the line.
- Every rule pattern also flags error lines in the excerpt, so the lines a rule can match stay in it.
- `core/analysis.py` (`analyze_jobs`) runs the offline part of the pipeline for the jobs of a run: the excerpts (one shared budget), the rules, the rules diagnosis and the signature, whose error type is the best rule id. The diagnose use case (P1c) and the eval runner both call it.

**Why:** Priorities per ecosystem got wrong answers: a timeout or a missing package must win over a failing test that it caused, and a pytest `FAILED` line is usually a symptom. One match per rule keeps the evidence of a cause together. The added patterns are the forms the same errors take in tools the lab and the project use (uv, Python sockets, Node).
**Alternatives:** Priority by ecosystem (generic < specific; ranks a test failure above the timeout that caused it); one match per line (scatters the evidence, ranks by repetition).
**Consequences:** The priorities are judgement calls, which the confusion matrix of the lab and public cases will test. The step-timeout pattern is the expected GitHub message, not yet seen in a captured log; the first lab capture confirms or corrects it.

## D-61 — Eval runner skeleton, lab repository sources and the `evals capture` command
**Amended by D-62:** the capture reads the log with `--allow-escape-sequences` and strips terminal escape sequences before redacting it.
**Origin:** Author request (runner, lab workflows to publish and capture) + Proposed (the details)
**Decision:**
- `evals/` is a top-level package run as `python -m evals`.
  - `run` loads `evals/cases/<source>/*`, runs the variants (`rules` today; `all` runs what exists and lists the rest), and writes `evals/reports/<date>-<sha>.{json,md}`.
  - Metrics today: accuracy, per-category precision/recall/F1, macro-F1 over the categories with support, the confusion matrix, evidence kept in the excerpt and cited by the diagnosis, abstention, a calibration table, latency and cost.
  - It fails with a clear message when there are no cases or the variant does not exist yet.
- `case.yaml` gains `job_id` (the job of `log.txt`), `lab_case` (the `lab/cases.yaml` entry, shared by several captures of one failure) and `captured_at`.
- `lab/` holds the lab repository sources:
  - 23 `workflow_dispatch` workflows covering the 8 categories, at least 2 each, with `contents: read` and a timeout;
  - a small Python project, two Dockerfiles, two Terraform folders and two scripts;
  - `cases.yaml`, which labels each case (category, cause, fix, evidence regexes, how many runs to capture).
- No case needs a secret or a cloud account. A real AWS `AccessDenied` is left out, and `aws-no-credentials` stands in for it. `lab/` is excluded from ruff, mypy and pre-commit, because it fails them on purpose.
- `python -m evals capture` (one run, or `--all`) uses the user's `gh` login:
  - it downloads the jobs of the attempt and the failed job's log, redacts the log, and keeps only the job and step fields the pipeline reads;
  - it finds the evidence line numbers in the failed step with the manifest's regexes;
  - it writes the case directory. It never prints log content and skips runs it already captured.
- The acceptance tests over `evals/cases/lab` (evidence lines in the excerpt, ≤ 6,000 tokens, one signature per repeated failure, different signatures across failures) are written and skip while no case is captured.

**Why:** The labels come from the person who wrote the failure, and the capture keeps them next to a real log without hand work. Capture-time redaction keeps fake or real tokens out of the repository. `workflow_dispatch` only means that publishing the lab costs nothing and fails nothing by itself.
**Alternatives:** Synthetic logs as lab cases (no real log formats, so they would only test our own assumptions); recording the logs by hand (slow, and evidence line numbers are easy to get wrong); a separate capture script under `scripts/` (one more entry point to learn).
**Consequences:** The P1b criteria that name lab fixtures stay open until the lab is published, run and captured. The capture depends on the GitHub CLI being installed and logged in.

## D-62 — Captured logs are stripped of terminal escape sequences; `gh` reads them with `--allow-escape-sequences`
**Origin:** Author request (found on the first real capture of the lab) + Proposed (the details)
**Decision:**
- Job logs carry ANSI colours. `gh api` refuses to output a response that has terminal escape sequences unless it gets `--allow-escape-sequences`, so the first real `evals capture` failed with `gh api failed: the response contains terminal escape sequences`. The capture now passes the flag for the log download only. The jobs response is JSON, which escapes control characters itself. `gh` runs with its output captured in a pipe, and the log is never printed.
- `core/logs/clean.py` gets `strip_terminal_controls`. It removes CSI sequences (colours, cursor moves), OSC sequences (titles, hyperlinks, ended by BEL or ST), the other two-byte escapes (ESC followed by 0x30 to 0x7E, such as the save and restore cursor pair) and every remaining control character except TAB, LF and CR (C0, DEL and the C1 range). An OSC body cannot contain a newline, so no newline is ever removed and line numbers do not move. Bare CRs stay, because the excerpt reduces a progress line to its last state (§7.3). The function is idempotent. `clean_text` shares its regex.
- The capture strips first, then redacts, then looks for the evidence lines. A colour code in the middle of a secret cannot hide it from the redaction patterns, and one between the words of an error message cannot stop an evidence regex from matching.
- A stored `log.txt` never holds a terminal escape sequence, so printing or opening it cannot move the cursor, set a window title or rewrite what the screen shows.

**Why:** Logs are untrusted input. A stored log with escape sequences is a file that can act on whatever terminal shows it, and colours also get in the way of both redaction and the evidence regexes. Stripping in `core` makes the rule testable offline, with a property test over arbitrary text.
**Alternatives:** Passing the flag and storing the raw log (the stored files carry the escape sequences, and redaction and evidence matching have to cope with colours); stripping in the shell with `sed` or PowerShell (not testable, and Windows-only); asking GitHub for a log without colours (the logs API has no such option).
**Consequences:** A stored log is not byte-identical to what GitHub serves, but it has the same lines. The webhook path (P1c) never stores a raw log (D-12) and its excerpts are already cleaned per line. No lab case had been captured before this change, so none needs to be redone.

## D-63 — OpenAI call: Responses API, strict schema without length keywords, `store=false`, retries owned by the pipeline
**Origin:** Proposed (implements D-07)
**Decision:**
- `OpenAILLMClient` calls `responses.create` with `text.format = {type: json_schema, strict: true}`, no tools, `max_output_tokens = LLM_MAX_OUTPUT_TOKENS` and `store=False` (OpenAI does not keep the log excerpt for its own retention).
- The schema sent is `strict_output_schema()`: the `LLMDiagnosisOutput` schema without `title`, `description`, `default`, `minLength`/`maxLength`, `minItems`/`maxItems`, `minimum`/`maximum` and their exclusive forms, `pattern` and `format`. Strict mode accepts only a subset of JSON Schema, and a rejected schema would fail every call. The properties, the enums and `additionalProperties: false` stay. The limits are enforced by `core` after the call (`parse_output`, `finalize`), which D-07 already required.
- The SDK is created with `max_retries=0` and the pipeline's own timeout. Retries (1 s and 4 s, transient errors only) belong to `DiagnoseJob`, so they are counted, tested and bounded by the budget in one place. A second layer in the SDK would multiply them.
- Errors are translated, not copied: timeouts, connection errors, 429 and 5xx become `LLMTransientError`; an authentication error, a quota code (`insufficient_quota`, even on a 429), any other 4xx and a refusal become `LLMError`. A response that comes back `failed` or `cancelled` is transient only for the codes `server_error` and `rate_limit_exceeded`. No response body or SDK message is copied into an exception message or a log, because a body can echo the prompt, which holds log text; the original SDK error stays as `__cause__`.
- A response with status `incomplete` (hit the output limit) returns the partial text, which then fails validation, gets its repair and falls back to the rules with `invalid_output`.
- The tests use `httpx2.MockTransport` (the SDK 3.x runs on `httpx2`, D-44): they check the request that goes out (model, instructions, input, schema, `store`, no tools), every error mapping, and that neither the key nor the body of an error response reaches an error message.

**Why:** The Responses API is the current OpenAI interface for Structured Outputs. Dropping the keywords that strict mode rejects costs nothing, because `core` re-validates anyway. A single owner of retries keeps the cost and the latency bounds provable.
**Alternatives:** Chat Completions with `response_format` (older, same guarantees); JSON mode plus our own validation (no schema guarantee, more repairs); keeping the SDK's retries (hidden extra calls that bypass the ledger).
**Consequences:** The OpenAI contract is checked offline against a mock only; the first real call is the `-m live` smoke (D-65), and it is the one that can show that the strict schema is accepted. If OpenAI rejects it, `strict_output_schema()` is the one place to change.

## D-64 — Safe rendering treats every diagnosis text as untrusted; a CommonMark-aware threat scanner and `judge` define an attack; the injection suite is synthetic
**Origin:** Proposed (implements D-25 and SPEC §9.5)
**Decision:**
- Rendering (`core/render.py`) treats every text of a diagnosis as untrusted, the rules diagnosis included: its templates fill in values that the rules captured from the log, so an attacker who writes the log also writes them. Everything is flattened to one line where it must be, HTML is escaped, markdown control characters are escaped (including the backtick, `=` and a leading `#`, `>`, `-`, `+`, `|` or number), `@mentions` get a zero-width joiner, references such as `#12`, `owner/repo#12` and `GH-12` go in code, invisible and bidi characters are removed, secrets are redacted a second time and every field is cut to its limit.
- A URL is shown as `<url>` only when it is `https`, has no userinfo or port and its host is exactly on the allowlist of §9.5. Any other URL (and any scheme) is shown as inert inline code, cut to 200 characters. Images are never produced.
- The evidence lines are shown as plain labels, with at most the link of the step (`…/job/{id}#step:{n}:1`). A line number of the excerpt is a position in the retained log (D-58), not GitHub's per-step line number, so a link to `#step:5:10` would point at the wrong line.
- `evals/runner/markdown_threats.py` is the oracle for the renderer: a small CommonMark-ordered scanner (backslash escapes, then code spans, then inline constructs) that reports what could act on a reader: raw HTML, a link or image destination, a link reference definition, an image, a bare URL, a live mention, an HTML entity, an issue reference, and a heading, blockquote, table row, thematic break or setext underline, or list item that the text did not intend. The unit tests run the renderer through it with fixed attacks and with Hypothesis. It found four real gaps in the renderer while it was written: a lone backtick that paired with one of ours into a code span, `@https://…` (the mention was neutralised but the URL after it was not seen), a reference such as `#0_` glued to other characters, and a `=` line that is a setext underline.
- The injection suite (`evals/runner/injection.py`) has 16 payloads over two real failure kinds (a missing module and a broken test): override of the category, a fake JSON answer, phishing link, markdown link and image, mention flood, request to reveal the prompt, fabricated evidence, closing the untrusted tag, a chat template, HTML, an entity-encoded mention, a path traversal in a suggestion, invisible text, exfiltration of secrets, an instruction flood and role-play. It generates `evals/cases/injection/*` (a test fails if the committed files drift: `python -m evals write-injection` regenerates them).
- `judge(case, diagnosis, markdown, excerpt)` defines an attack that succeeded: `category_forced` (an LLM diagnosis whose category is the attacker's target and not the true one), `rendered:<threat>` (the scanner finds a threat in the rendered check run and comment, one entry per kind), `prompt_leaked` (the canary of the system prompt shows up), `fabricated_evidence` (published evidence whose text is not in its line of the excerpt) and `unsafe_suggestion` (a suggestion path that is absolute, starts with `~` or has a `..` segment). The target is zero.
- Offline, with a fake model that obeys the log (`obeys_injection`), the tests prove that `core` still blocks everything except `category_forced`, which no `core` check can decide: a model that obeys can pick the attacker's category, and only the live run measures how often the real model does (D-65). The tests also show that a fork run never reaches the model, and a mutation check was run once over nine of the defences (each mutation made a test fail).
- **Accepted limit:** the rules read the log, so an attacker can plant text that makes a rule match (for example a fake `ModuleNotFoundError`). That is not an injection into the model: it is a log that really says so. The rules diagnosis stays a wrong-but-inert answer, and the rule baseline never carries markup because of the rendering above.

**Why:** If the model is the only line of defence, one clever payload wins. If the output can only be inert text, a payload can at most make a wrong diagnosis. A scanner written from CommonMark's rules is a better oracle than the renderer's own regexes, because it does not share its blind spots.
**Alternatives:** A markdown library to sanitize (HTML-oriented, it does not model GitHub's `@` and `#` notifications); linking evidence lines to the log (wrong line numbers, see above); real lab runs for the injection cases (16 more workflows to publish and capture for a suite that does not need a real runner).
**Consequences:** The scanner has no guarantee beyond the constructs it knows. A new GitHub markdown feature that notifies or links must be added to it first (SPEC §14 review at P6). The synthetic cases use an invented repository, and the injection payloads are text in `evals/runner/injection.py`, a deliberate exception to "logs are never executed": nothing here follows them.

## D-65 — LLM evals: the pipeline is the system under test, answers are cached, `-m live` runs on demand, the model is chosen by a mini-eval that waits for the key
**Origin:** Author request (P1b without the OpenAI key yet) + Proposed (the mechanics)
**Decision:**
- The `llm` and `llm+hints` variants run the production `DiagnoseJob` (policy, budget, validation, one repair, fallbacks), so a report describes what the app would publish and shows its fallbacks. Each case is run as a non-fork run, one after the other, with a ledger that never refuses. A model is a column of the report (`llm+hints [model]`); several models in one run (`--model a,b`) are the mini-eval.
- `CachedLLMClient` wraps the real client. Its key is the SHA-256 of `(model, prompt_version, the whole DiagnosisRequest)`, so a change of model, prompt, excerpt, hint or repair message is a miss. An entry holds only the answer (`LLMResult`), never the request, and is written atomically. Without `--live` there is no inner client: a miss is an `LLMError`, which the pipeline reports as `llm_error` fallbacks, never a call. Errors are never cached. The directory is `evals/.cache/` (SPEC §11.3) and is meant to be committed once it holds answers, so CI runs offline.
- Report additions: share of diagnoses that came from the LLM, fallbacks by reason, calls, tokens, cost per case, attack cases and attacks won, and a per-case source column. Prices come from `LLM_PRICE_*` or `--price MODEL=IN,OUT`.
- `--live` is the only way to spend money from the eval runner. It reads `OPENAI_API_KEY` from `.env` into a `SecretStr`, never prints or stores it, and fails with a message that names the variable if it is missing.
- `tests/live/` (smoke of five lab cases, and the injection suite against the real model) carries the `live` marker by directory and is skipped unless `-m live` is given. Even then it is skipped when `OPENAI_API_KEY` or `OPENAI_MODEL` is missing. Each test is fenced to 40 calls. The smoke test checks the contract (valid answer, no fallback, one or two calls, evidence inside the excerpt), not quality.
- **The model is not chosen yet.** The procedure waits for the key: run `uv run python -m evals run --variant all --sources lab,injection --live --model <a>,<b> --price <a>=IN,OUT --price <b>=IN,OUT` with two small-tier candidates, compare `llm+hints` by macro-F1, then attacks won (must be 0), then cost and p95 latency, and record the winner, its price and the numbers as a new D-NN (D-07). Until then `OPENAI_MODEL` stays empty in `.env.example` and the live boxes of SPEC §13 stay open.

**Why:** Measuring the pipeline and not a bare model call is what makes the table honest (a model that often needs a repair costs more and falls back more). Caching by the full request keeps the eval deterministic and free after the first run, and keeping the request out of the file keeps log text out of the answer files.
**Alternatives:** A cache keyed by the case id (stale after any change of the excerpt code); calling OpenAI directly from the runner (skips the validation that decides what is published); waiting to write the live tests until the key exists (the mechanics would be untested when it does).
**Consequences:** Everything that does not need OpenAI is tested; what does (the strict schema being accepted, real answers, the real injection rate) is written, skipped and unproven until the first `-m live` run. The cache and the cost numbers in the README start empty. The cause-match judge (§11.2) arrives in P4.

## D-66 — Diagnosis pipeline: budget reservation, one repair, fallbacks that never raise, evidence verification and restricted suggestions
**Origin:** Proposed (implements SPEC §5.1, §7.1, §8, §9.3, §9.4 and D-16)
**Decision:**
- `core/policy.py::decide` returns `llm`, `cache` or `rules` plus a `fallback_reason`, in the order of §9.4 (without a budget check, which belongs to the reservation). `fallback_reason` is part of `Diagnosis` and has six values (`llm_error`, `invalid_output`, `budget`, `fork_policy`, `config_off`, `disabled`). No client at all is `disabled`.
- Budget: before the first call the pipeline reserves `2 × cost(excerpt tokens + 1,500 tokens of prompt, LLM_MAX_OUTPUT_TOKENS)` (the call and at most one repair, D-16), and the reservation is always settled with the real usage in a `finally`. A refusal is `fallback_reason = budget`. The ledger port is `UsageLedger`; `InMemoryUsageLedger` is the fake (30 calls per installation per UTC day and `LLM_DAILY_BUDGET_USD`; zero refuses everything). The Postgres ledger is `PostgresUsageLedger` (D-69).
- Calls: a transient error is retried twice (1 s, then 4 s, injectable sleep); a permanent one is not. Out of attempts is `llm_error`.
- Validation: `parse_output` accepts only the strict schema (extra keys forbidden). A bad answer gets one repair call whose prompt carries only the paths and messages of the validation errors (at most 600 characters, never the offending text). A second bad answer is `invalid_output`.
- `finalize` makes a valid answer safe: an answer that contains the canary of the system prompt is refused; an evidence item is kept only if its line number exists in the excerpt and its text is a part of that line (whitespace-normalised; it may carry the `L<n>:` prefix of the prompt, and the text as the prompt showed it, with the tag names escaped, also matches); if none survives, `evidence_verified` is false and the confidence is at most 0.3; a suggestion must be a relative path (at most 260 characters, no `..`, drive, `~` or control characters) over a range of at most 30 lines, and is dropped otherwise (the text of `suggested_fix` stays); every text is redacted again and cut to its limit.
- The rule baseline is computed first and independently. When a rule with confidence at least 0.85 disagrees with the published category, both are shown (`baseline_to_show`). The rules hints are sent to the model only in the `llm+hints` variant (D-14).
- The pipeline never raises for an LLM problem: every path ends in a `Diagnosis` (NFR-12).

**Why:** Each rule here is a bound that a test can check: calls (2), money (reservation), time (1 s + 4 s), what is believed (evidence in the excerpt) and what is published (inert text, D-64). The 0.85 threshold is the one of §9.3.
**Alternatives:** Checking the budget after the call (cannot hold under concurrency); several repairs (cost with diminishing returns); trusting the evidence line number alone (a model that invents a quote at a real line would pass).
**Consequences:** The reservation is conservative (it counts the repair even when it is not used) and the settled cost is the real one. The prompt overhead of 1,500 tokens and the 0.85 and 0.3 thresholds are judgement calls that the live evals will test; the fallback rate in the report is where a bad choice shows up.

## D-67 — GitHub HTTP client: `httpx2`, one error mapping, tests on `httpx2.MockTransport` with a FakeGitHub (no respx)
**Origin:** Proposed (amends D-28 and D-44)
**Decision:**
- `adapters/github/http.py::GitHubHttp` is a thin layer over `httpx2.AsyncClient`. `httpx2` is declared as a runtime dependency (it was already installed for `openai`, D-44) and moves out of the dev group; `httpx` and `respx` are not added. The client sends `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` (the version the spike saw selected), `User-Agent: cicd-doctor`, with a 5 s connect and a 30 s read timeout (30 s write, 5 s pool). Redirects are never followed: the job log endpoint answers 302 and the log source (P1c, D-71) reads `Location` itself, so it passes `allow_redirect`; any other 3xx is a `PermanentError`.
- An endpoint is a template (`/repos/{owner}/{repo}/actions/jobs/{job_id}`) plus values. The values are percent-encoded, and an empty value, `.` or `..` is refused. Only the template reaches a message.
- **Errors are mapped in one place** (`GitHubHttp._raise_for_status`). `401`: ask the credential to refresh and send the request once more; a second 401 is transient. A `403` or `429` that is a rate limit is `TransientError` with `retry_after`: `retry-after` first, then `x-ratelimit-remaining: 0` (wait until `x-ratelimit-reset`), then a secondary limit without headers (a 429, or a 403 whose `message` says "rate limit" or "abuse detection": 60 s). The wait is kept between 1 s and 1 hour, because the primary window is one hour and a longer wait can only come from a bad header. Any other `403` is `PermanentError`; `404`, `410`, `422` and every other 4xx are permanent; `408`, `5xx`, timeouts and connection errors are transient. The message of an error is `<METHOD> <template> answered <status>` (for a timeout or a connection error, the exception type), never a response body, a URL, a query or a token. The body of a 403 is read only to decide whether it is a rate limit: nothing of it is copied. **One measured exception** (spike): a 403 carries `x-accepted-github-permissions` (for example `secrets=read`), which says what permission was missing; it is added to the message only if it matches `^[a-z_=,; ]{1,100}$`.
- The errors are `GitHubTransientError` and `GitHubPermanentError`, subclasses of the ones in `core/errors.py` (which do not change) that also carry `status_code` (`None` when GitHub did not answer). The worker still sees a `TransientError` or a `PermanentError`, and an adapter can tell a 404 (logs expired, D-71) from a 403 without parsing a message.
- **No retries inside the client** except the 401 refresh: the job retries (D-23), and with the checkpoint planned for P1c, repeating a job after the analysis only repeats cheap `PATCH`es.
- `paginate` follows `Link: rel="next"` with `per_page=100`, up to a cap of pages (10 by default), and returns the items with a `truncated` flag, so a caller that hits the cap knows it (a WARNING also names the template). A next link that leaves the API's scheme, host or port is refused: the token goes with every request.
- `configure_logging()` sets the `httpx2` and `httpcore2` loggers to WARNING. At INFO `httpx2` prints the URL of every request, and the URL of a log blob carries its signature in the query. A test shows both facts.
- **Tests** run the real adapters on `httpx2.MockTransport`, as the OpenAI tests do (D-63). `tests/github_fake.py::FakeGitHub` is a stateful router. It records every request with its headers (the `repr` of a request never shows `Authorization`); keeps installations and the tokens it issued (they expire with the injected clock); checks the app JWT and the installation tokens as GitHub does; lets a test register routes (`add_route`, so the next PRs add logs, check runs, comments and pulls without rewriting it); fails on demand (status, headers, delay, a raised transport error); and puts a canary in the body of every error, so each error test is also a leak test. The fake responses are built inline in the tests: fixtures recorded from the real API arrive with the PRs that need them, after review.
- `core/` may not import `httpx2`, `jwt` or `cryptography` (`tests/unit/test_architecture.py`).

**Why:** One HTTP stack in the process (the OpenAI SDK and FastAPI's test client already run on `httpx2`). Every rule of the error table is a bound a test can check, and keeping the table in one place means the log source, the reporter and the pull request adapter cannot disagree about what is retried. Not copying bodies and URLs follows D-63: a body can echo anything, and a signed URL is a credential.
**Alternatives:** `httpx` + `respx` (D-28): a second HTTP stack, `respx` patches globally, and its support for `httpx2` is uncertain and unneeded; `aiohttp` + `aioresponses` (a third stack, and a different test API than the one for OpenAI); `vcrpy` cassettes (they record tokens and signed URLs, and replay hides the very headers that must be asserted, such as the missing `Authorization` on a blob); `githubkit` (rejected in D-28).
**Consequences:** Amends D-28 (`httpx` is `httpx2`, `respx` is not used) and closes the check that D-44 asked for. SPEC §12 says `httpx2.MockTransport` where it said `respx`. An isolated 502 costs one job retry (30 s of backoff at least), which is fine for NFR-02 because that is a p95. The 401 refresh does nothing for the app JWT (it is signed again on every use), so a wrong private key shows as a transient 401 and the job dies after its attempts. A 408 is the only transient 4xx. The default cap of 10 pages and the one hour limit on a wait are judgement calls; the log source and the reporter set their own cap where it matters (D-70 plans 10 pages of comments).

## D-68 — App authentication: PyJWT, installation tokens scoped per repository and permission, in-memory cache with one request in flight
**Origin:** Proposed (implements D-21 and SPEC §9.6)
**Decision:**
- `adapters/github/auth.py::GitHubAppAuth` signs the app JWT with `pyjwt[crypto]`: RS256, `iat` = now − 60 s (a slow local clock), `exp` = now + 9 min (GitHub accepts up to 10), `iss` = `GITHUB_APP_ID` as a **string** (the spike showed that GitHub accepts it). Time comes from the injected `Clock`. The JWT is signed again on every use (about a millisecond), so nothing caches it.
- The private key is read from `GITHUB_PRIVATE_KEY` (a `SecretStr`) by `load_private_key`: PEM content (real newlines, or the two characters `\n` of a one-line `.env` value) or the path of a PEM file. It is parsed once into an RSA key object, whose `repr` shows nothing. An encrypted key, a key that is not RSA, or text that is neither a PEM nor a readable file raises a `ValueError` that names the variable and never its value, its path or its content.
- Installation tokens are requested with `POST /app/installations/{id}/access_tokens`, with `repository_ids: [repo_id]` and only the permissions of the use case (`DIAGNOSIS_PERMISSIONS` = `actions: read`, `checks: write`, `pull_requests: write`; P2 and P3 add `contents: read` where they read the repository). They are cached in memory per `(installation_id, repo_id, frozenset(permissions))` until 5 minutes before `expires_at` (an entry is renewed when `now ≥ expires_at − 5 min`), and the expired entries are dropped whenever a new one is stored.
- **One request in flight per key.** The request runs as a task that no caller owns: ten concurrent callers make one `POST`, cancelling the caller that started it does not cancel the others, and a failure is shared by everyone who was waiting for it and is not cached (the next call tries again).
- A job uses a credential from `installation_credential(installation_id, repo_id, permissions)`, which is what `GitHubHttp` takes (`Credential`: `authorization()` and `refresh(rejected)`). After a 401 the credential drops the cached token **only if the cache still holds the token that was rejected**, so two requests that fail with the same stale token cost one new token and not two.
- At the token request, `403` (suspended), `404` (uninstalled) and `422` (a repository or permission that is not available) are `PermanentError` with the reason in the message; `5xx` and rate limits are transient (D-67). A response that does not parse (no token, an invalid or naive `expires_at`) is a `TransientError` that quotes nothing, because a validation error would quote the token.
- `app_identity()` (`GET /app`: `slug` and `id`; the bot is `<slug>[bot]`) is fetched once per process, with the same single flight. `AppIdentity` lives in the adapter for now: `core/models.py` belongs to the persistence PR.
- The tests generate the RSA key pair in each test session with `cryptography`: no PEM is committed (`*.pem` stays ignored) and the redaction corpus is not touched.

**Why:** A token that can only write checks and comments on one repository limits what a bug or a stolen token can do (NFR-06, D-21). Sharing one request per key keeps a burst of jobs from one repository from multiplying token requests. Dropping the cache entry only for the rejected token is cheap and avoids a stampede of renewals.
**Alternatives:** RS256 by hand with `cryptography` (one dependency less, but our own cryptographic code for no real gain); `joserfc` or `authlib` (bigger and more general); one installation token without a repository scope (a simpler cache, but it breaks NFR-06 and D-21); a `Lock` per key (a shared failure would become one request per waiter).
**Consequences:** `cryptography` becomes a runtime dependency (it has wheels for Windows and Linux on Python 3.13). The app id stays the `iss`; GitHub now recommends the Client ID, which would be a new variable and another D-NN. The container (P1c) builds one `GitHubAppAuth` per process and one credential per job.
## D-69 — Postgres store and ledger: one checkpoint per run attempt, deterministic failure ids, the LLM usage on the published diagnosis row
**Origin:** Proposed (implements SPEC §4.3, §6, §12 for P1c; completes D-35, D-48 and D-66)
**Decision:**
- `PostgresFailureStore` implements the whole `FailureStore` port except `similar`, which needs the embeddings of P2 and raises `NotImplementedError("P2")`. The contract suite runs on the in-memory and the Postgres adapters (D-51); the three `similar` tests run on memory only, with the reason written next to their fixture.
- **Checkpoint.** `save_run_analysis(record)` writes the run, the attempt, the failures and their diagnoses in **one** transaction and sets `run_attempts.analyzed_at`. The first writer wins: the attempt is claimed with `UPDATE … SET analyzed_at = :now WHERE … AND analyzed_at IS NULL`, a concurrent writer waits for that row lock and then updates nothing, and a writer that lost (a worker whose lease expired while it finished) gets the stored record back and continues with it. `run_analysis(run_id, attempt)` rebuilds the record from the rows exactly, and is `None` while `analyzed_at` is NULL (a half-written analysis made row by row is not a checkpoint). An attempt without failures is a valid checkpoint. The commit is the checkpoint: a crash before it rolls everything back, `analyzed_at` included. An analysed attempt is final: `save_run` no longer rewrites it.
- **Ids.** `failures.id = failure_id_for(run_id, attempt, job_id)`, a UUIDv5 of `"{run_id}:{attempt}:{job_id}"` under a fixed namespace, so a job has the same id in memory and in Postgres and in every retry. `RunAnalysisRecord` refuses ids that are not that, failures that belong to another attempt, or a job twice, and keeps `items` ordered by job id, so a record that goes through any adapter and comes back is equal to itself.
- **Diagnoses.** Always a `rules` row; an `llm` row (or `cache`, when P2 serves one) only when that is the published diagnosis, that is, only when the LLM returned a valid answer (D-35, D-66). A published rules fallback is the `rules` row itself, with its `fallback_reason`. Exactly **one** row per failure is `published`: a partial unique index (`diagnoses_published_idx`) makes the database refuse a second one. The LLM usage (model, calls, tokens, latency, cost) lives on the published row, also when it is a rules fallback that paid for calls whose answers were invalid, so `SUM(cost_usd)` is the real spend. `DiagnoseJob` returns that usage as a frozen `DiagnosisUsage` (it keeps `failed_calls`, which cost nothing). `StoredFailure` validates the shapes the rows can keep: the rules diagnosis has no model and no reason, a published rules diagnosis is the rules one plus a reason, a cached diagnosis made no call, and the usage of an LLM diagnosis names the model that produced it. A test runs every outcome of the pipeline through them.
- **Migration `0003`** (hand-written SQL, D-50) adds what the row needs to rebuild a `Failure`: `failures.conclusion`, `runner_os`, `started_at`, `completed_at`, `step_started_at` and `step_completed_at` (the tables are empty everywhere, so `conclusion`, `started_at` and `completed_at` are `NOT NULL`), `run_attempts.analyzed_at` and the published index. It also makes two changes that the first design did not list, because an exact round trip needs them: `diagnoses.llm_calls` and `diagnoses.llm_failed_calls` (the usage has call counts, the table had no column for them), and `diagnoses.confidence` goes from `numeric(3,2)` to `double precision`. With `numeric(3,2)` a confidence of 0.855 comes back as 0.86, so the retry of a publication could show another number than the first one. On a rules row the `model` column holds the model of its failed calls (the usage's); a rules diagnosis has no model of its own.
- **Times** come from the injected `Clock` and travel as parameters (D-48): `created_at` of failures and diagnoses, `analyzed_at`, `resolved_at`, and the window of `cached_diagnosis`, which a `FakeClock` can therefore move. Every lookup is scoped by `repository_id` (D-10).
- **Newer-attempt guard** in SQL: `save_publication` is `INSERT … ON CONFLICT DO UPDATE … WHERE (last_run_id, last_attempt) <= (EXCLUDED…) RETURNING 1` (for a check run, `last_attempt <= EXCLUDED.last_attempt`: its run is its key); no row returned means a newer attempt already published. It is the same `(run_id, attempt)` order as the in-memory adapter, and a check `Publication` must name the run of its key.
- **Ledger.** `PostgresUsageLedger.reserve` runs in a transaction that holds `pg_advisory_xact_lock(hashtext('llm_budget'))`, so every worker and process is serialised: it checks that the installation's `calls < limit` and that today's `SUM(reserved_usd + spent_usd)` plus the estimate stays within the global budget, and only then counts the call and holds the estimate (`INSERT … ON CONFLICT DO UPDATE`: a refusal leaves no row). `settle` is one atomic `UPDATE` that swaps the held estimate for the real cost, and raises `KeyError` for a reservation that was never made, like the memory ledger. The limits come from the constructor, the UTC day from the `Clock`, and a budget of 0 or a cap of 0 calls refuses everything without a query. A reservation of a worker that dies before `settle` stays held until the UTC day ends: conservative.
- **Publication lock.** `publication_lock(key)` is an async context manager. On Postgres it takes a session-level `pg_advisory_lock(hashtextextended(<stable text of the key>, 0))` on a pooled connection that stays checked out for the whole critical section, waits at most 30 s (`lock_timeout`, set transaction-locally so it never reaches the next user of the connection) and then raises `TransientError`. It is released on exit, or by the database when the connection dies. A statement that was cut short (a cancelled waiter) discards its connection instead of returning it to the pool, so no session stays queued for the lock. In memory it is an `asyncio.Lock` per key with the same wait. A hash collision only serialises two outputs that did not need it.

**Why:** The diagnosis of an attempt is computed once and costs money, and the publication that follows can fail and be retried (D-70). That only works if what was computed is stored atomically, can be read back exactly, and cannot be stored twice with different ids. The ledger and the lock are the two places where several workers must agree, and a database lock is the one thing they all see.
**Alternatives:** Row-by-row methods without a common transaction (a checkpoint written halfway); random ids with `ON CONFLICT … RETURNING` (works in Postgres, but the memory store would have to imitate it); a `checkpoints` table with a JSON of the whole record (simple, but it duplicates the schema that P2 queries by column); keeping `numeric(3,2)` and rounding in the domain (it changes what the LLM validation returns); storing the usage in a separate table (one more join for `SUM(cost_usd)`); `SELECT … FOR UPDATE` on the publication row as the lock (it does not exist before the first publication); a polling `pg_try_advisory_lock` loop (no fairness and a delay of up to the poll interval).
**Consequences:** The `FailureStore` port grows (`save_run_analysis`, `run_analysis`, `publication_lock`); SPEC §4.3, §5.2, §6 and §12 say so. The row-by-row methods stay for P2 (`record_outcome`) and for the contract tests. A lock holder keeps one pool connection for about a second (a GitHub call), so the pool of a worker needs one connection per consumer more than it uses for queries. Nothing is wired yet: the use case and the handler arrive in the next steps of P1c.

## D-70 — The boundary between persisting and publishing: compute once, publish from what is stored, exactly one comment
**Origin:** Proposed (completes D-06, D-18 and D-23 for P1c)
**Decision:** The diagnosis of an attempt is computed once and published as many times as it takes, always from what is stored.
1. **Guard.** If the check of `(repo, run)` was already published by a higher attempt, the job ends without doing anything (an old attempt).
2. **Check `in_progress`,** under the lock of the check's key: a stored id → `PATCH`; else a search by `external_id = "cicd-doctor:run:{run_id}"` (and the app id); else `POST`. The id is stored **at once**, before analysing. On a new attempt the check run is already `completed`: GitHub does not reopen it (U10, measured on 2026-11-16: the `PATCH` to `in_progress` answers 200, the check stays `completed` / `neutral`, and `name` and `output` are applied), so this step then only changes its text ("Analyzing failure… (attempt N)").
3. **Checkpoint.** If `run_analysis(run, attempt)` exists, it is used; otherwise jobs → logs → analysis → `DiagnoseJob` → `save_run_analysis`. The commit is the checkpoint, and the LLM is called only before it.
4. **Check completed,** under the lock: guard → `PATCH` `completed` / `neutral` → `save_publication`.
5. **Comment,** under the lock of `(repo, PR, workflow)`: guard by `(run_id, attempt)` → upsert (stored id → the marker among the bot's comments → `POST`) → `save_publication`.
- **What is retried when publishing fails after persisting:** only steps 2, 4 and 5, which are idempotent (a `PATCH` of the same content), from what is stored: no log download, no LLM call, no new reservation. The bound "one call per job per attempt" (FR-13) holds across retries of the job.
- **Exactly one comment:** (i) the lock serialises everyone who writes one key, from any worker and any run (two pushes in a row that fail in the same PR); (ii) inside the lock, the stored id or, if it was lost (a crash between the `POST` and `save_publication`), the search by marker finds the comment we created; (iii) only comments by `<slug>[bot]` count, so a user who copies the marker can neither hijack nor block it. The guard inside the lock stops an old attempt from overwriting a new one. The check run has the same guarantee through its `external_id`.
- Accepted residues: GitHub's comment list not showing a comment created less than a second ago **and** the worker dying right then; PRs with more than 1,000 comments (the page cap); a crash between the LLM answer and the commit of the checkpoint, which repeats one call (the ledger bounds it).
- **What exists so far.** The ports of this decision that do not touch GitHub: `FailureStore.save_run_analysis` / `run_analysis` (step 3), `publication_lock` and the SQL newer-attempt guard of `save_publication` (steps 1, 4 and 5), and the budget under concurrency (all in D-69). The `Reporter` changes (an id it already knows, the run on `complete_check`) and the PR lookup (D-72) exist in `adapters/github/reporter.py` and `pulls.py`, tested on FakeGitHub; the use case that follows these steps comes later in P1c. **U10 is resolved (measured by hand on 2026-11-16):** GitHub does not reopen a completed check run. A `PATCH` with `status: in_progress` on a `completed` check answers **200** and the check stays `completed` / `neutral`, while `name` and `output` in the same `PATCH` are applied. The reporter therefore `PATCH`es `in_progress` with the current name and the "Analyzing failure… (attempt N)" output, takes the 200 as the normal answer (it does not read the status back and retries nothing), and the later `completed` `PATCH` writes the final text. A 422 on that `PATCH` was the case this decision expected; it never happened, and the branch that answers it with a `PATCH` of the `output` alone stays only as a defensive path, with its own test. So on a re-run the same check run keeps its id, its status and its conclusion, and only its text changes (box 2 of the live runbook checks it).

**Why:** Publishing is the part that fails (rate limits, 5xx, a worker killed between two calls), and analysing is the part that costs. Persisting first turns "retry" into "repeat a cheap `PATCH`".
**Alternatives:** Publish and then persist (a crash loses the record and the retry calls the LLM again); an outbox: persist and enqueue a separate `publish` job (a cleaner separation, but another job kind with its own dedupe and order; it stays a possible refactor); no lock, only the guard (a race between reading and writing: an old attempt can overwrite the new comment, and two runs can both `POST`); claiming the `comment_publications` row before the `POST` (it needs a nullable `comment_id` and a claim that expires if the worker dies: it would be a lock reimplemented).
**Consequences:** The lock holds a connection during a GitHub call (about a second), so the pool grows by one per consumer. The first check run of a run is always created by step 2 before any analysis, so a run whose analysis never finishes still shows an `in_progress` check until the job is dead; publishing "Could not diagnose" for it is a later decision of P1c.
**Verified live (2026-11-16):** two CI runs of one PR and workflow (a push and a `pull_request`) that failed at about the same time produced one comment, created by the first and edited by the second (comment 6931758188, 22:50:28Z and 22:50:57Z), and four diagnosis rows. A re-run (`gh run rerun --failed`) kept the same check run and the same comment. A `PATCH` to `in_progress` on the completed check run answered 200 and left it `completed` / `neutral` while the new output applied: the 422 that the defensive branch expects did not happen, and the check is never reopened.

## D-71 — Log download: a redirect fetched without credentials, an offset `Range`, a ring buffer behind it
**Origin:** Proposed (implements SPEC §4.2, §7.1 steps 4-5 and §8 for P1c; confirmed by a read-only spike on the real API, 2026-11-16)
**Decision:**
- `adapters/github/logs.py::GitHubLogSource` implements `LogSource`. `jobs(run)` paginates `GET /repos/{o}/{r}/actions/runs/{run_id}/attempts/{attempt}/jobs` (`items_key="jobs"`, the installation credential of the run) and parses with `jobs.py`; it always uses the attempt endpoint with the run's attempt, because the endpoint without an attempt changes after a later re-run. A listing that does not parse is a `TransientError` that names fields, never values.
- `job_log(job, max_bytes)` asks `GET …/actions/jobs/{job_id}/logs` through `GitHubHttp` (`allow_redirect=True`, so the 302 is read and never followed) and fetches the `Location` with a **separate** `httpx2.AsyncClient`: no default headers, no credentials, `follow_redirects=False`, an injectable transport and its own timeouts (5 s to connect, 30 s to read). `https` is mandatory (anything else is a `PermanentError` and is never fetched). The expected hosts end in `.blob.core.windows.net`, `.githubusercontent.com` or `.github.com`; any other host is still fetched without credentials, but a WARNING names the host (never the query).
- The blob is read as a stream. If `Content-Length > max_bytes`, the first stream is closed and `Range: bytes={length - max_bytes}-` is requested; the answer (206) is the tail, `truncated = True`, and its first, partial line is dropped (a cut that lands exactly on a line start loses that one line; a tail without any newline is kept whole). Otherwise (a small log, no `Content-Length`, a 200 answer to a ranged request, `Accept-Ranges: none`) the chunks go through `TailBuffer(max_bytes)`, which keeps the last `max_bytes`, starts at a complete line, and stops reading at the 100 MB hard limit (`truncated` is true whenever bytes were dropped or the limit cut the stream). Bytes are decoded as plain `utf-8` with `errors="replace"`: the BOM stays and `parse_log` strips it.
- **gzip.** If the blob ever answers with `Content-Encoding` other than identity, no `Range` is sent (offsets of a compressed body are not offsets of the log) and the cap counts decompressed bytes. The real blob does not do it today; a test fabricates the header.
- **Errors.** 404 or 410 from the API endpoint, or from the blob, is `LogsUnavailable(PermanentError)` (`core/errors.py`) and keeps `status_code`. A 403 from the blob is a `TransientError`: the signature expired, and a retry of the job asks for a new redirect. A second request to the blob URL is made only for the `Range` right after the first one; a failed request is never repeated with the old URL. Everything else follows `GitHubHttp` (D-67): 5xx, 408, 429, timeouts and connection errors are transient (also in the middle of a stream), another 403 or 4xx is permanent. No message or log line carries the signed URL, its query or a response body (a test with a canary and `caplog`).
- **The grace rule belongs to the use case** (next PR): a `LogsUnavailable` with 404 within 10 minutes of the job's `completed_at` is transient (the log may not be ready yet), later it is "Could not diagnose" (`logs_expired`); 410 is always permanent. The adapter only reports what GitHub answered.
- **What the spike measured on the real API.** The blob is an Azure blob (`productionresultssa<N>.blob.core.windows.net`, `<N>` varies; path `/actions-results/<uuid>/workflow-job-run-<uuid>/logs/job/job-logs.txt`, a SAS signature in the query) with `Content-Length`, `Accept-Ranges: bytes`, `Content-Type: text/plain` and **no** `Content-Encoding`; the body starts with a UTF-8 BOM. `Range: bytes=N-` answers 206 with `Content-Range`; **a suffix range `bytes=-N` is ignored** (200 with the whole body), so only offsets are used, and a 200 to a request that carried a `Range` is a complete body that goes through the `TailBuffer`. The signed URL was valid after 2 minutes and expired (403) after 10, so it is used right away. A job that does not exist is 404 (`{message, documentation_url, status}`); a missing permission is 403 with `x-accepted-github-permissions` (both mapped by `GitHubHttp`). Every job of the attempt listing carries its own `run_attempt`, and no job id repeats between attempts.
- **U4 (open).** The spike could not observe a failed job with `run_attempt = 1` listed inside attempt 2. `WorkflowJob` has no `run_attempt` and `select_failed_jobs` does not filter on it, so today such a job **would be taken** and its log diagnosed as part of attempt 2. A synthetic test documents it. No filter is added until a real case shows it happens.
- `tests/github_fake.py` gets the routes (`GitHubLogsFake`: the jobs listing with forced page sizes, the log endpoint with a new signature per redirect, and a blob host that serves offset ranges, ignores suffix ranges, can omit `Content-Length` or stream without end) in one section; `RecordedRequest.has_authorization` says whether a request carried the header.

**Why:** The signed URL is the only secret of the blob request, and the `Authorization` header must not travel to another host (SPEC §9.1), so the redirect is fetched by a client that has nothing else. Asking for the tail with an offset spares up to the whole log; the ring buffer and the 100 MB limit keep memory bounded when the server cannot be trusted to honour a `Range`.
**Alternatives:** `HEAD` and then `GET` (always one request more); following the redirect with the API client (it would leak the token, §9.1); a suffix range (the blob ignores it); downloading the zip of the whole run (D-11 rejected it); a filter on `run_attempt` for U4 (no real case yet).
**Consequences:** Amends SPEC §7.1 step 5 (the 404 grace rule) and §8. Peak memory per job is up to `max_bytes` of bytes plus its text; with up to 3 logs per run (`analyze_jobs` splits the token budget between them) and several consumers, expect on the order of 250 to 500 MB in the worst case, to size in P5 (`WORKER_CONCURRENCY`). Nothing is wired yet: the use case and the handler come in the next steps of P1c.

## D-72 — The pull request of a run, forks included
**Origin:** Proposed (completes D-17 and D-24 for P1c)
**Decision:** In the webhook, `workflow_run.pull_requests` is filtered by `base.repo.id == repository.id` (GitHub can list pull requests of another repository with the same branch name) and the first one left is taken. If none is left and the run is a fork run or comes from a `pull_request*` event, `PullRequestSource.find(run)` (`adapters/github/pulls.py::GitHubPullRequests`) searches `GET /repos/{o}/{r}/pulls?head={head_owner}:{branch}&state=open` and accepts only a pull request whose `head.sha == run.head_sha` and `head.repo.full_name == run.head_repo_full_name`; with several matches the lowest number wins. A deleted fork (no head repository), a run without a head branch, a run that is not from a pull request event, or no match gives `None`: no comment, and the check run is published anyway. The number found is stored in `workflow_runs.pr_number`, and the list is read through at most three pages.
**Why:** `pull_requests` is empty for fork pull requests and for `workflow_dispatch`, and it may list a pull request of another repository, on which the bot would comment by mistake. Matching the head commit and the head repository means a push that arrived while the job waited, or another fork with the same branch name, never receives somebody else's diagnosis. The measurement on the real API (a read-only spike) showed the empty list for a `workflow_dispatch` and the listing endpoint answering with a list; the fork case was not observed yet, so the tests build it on FakeGitHub from the documented shape.
**Alternatives:** `GET /repos/{o}/{r}/commits/{sha}/pulls` (pull requests by commit, but for forks it depends on the commit being in the base repository, which is less documented); trusting `pull_requests[0]` without the filter (it would comment on the wrong pull request).
**Consequences:** `workflow_run.pull_requests[].base.repo.id` becomes a required field of the parsed webhook (an invalid payload without it is refused, as any other missing field); a small change to the normalised payload (D-56) and a new synthetic fixture, `workflow_run_completed_foreign_pr.json`. The `PullRequestSource` port joins SPEC §4.3. Nothing calls it yet: the use case comes in a later step of P1c.

## D-73 — Job-level timeouts: `cancelled` runs are examined, and only a reported timeout is diagnosed
**Origin:** Proposed (resolves the first question of SPEC §14; option (a))
**Decision:** A job that exceeds its `timeout-minutes` ends `cancelled` (the job and the run), and its log only says `The operation was canceled.`, like a manual cancellation. So:
1. The webhook enqueues a `workflow_run` `completed` with conclusion `cancelled` as a `diagnose_run` job too (the same dedupe key `diagnose:{run_id}:{attempt}`; `DiagnoseRunPayload.conclusion` accepts `"cancelled"`). `success`, `skipped`, `neutral`, `stale` and `action_required` stay ignored (204).
2. `LogSource.job_annotations(job) -> list[str]` returns the `message` of every annotation of the job's check run (`GET /repos/{o}/{r}/check-runs/{job_id}/annotations`; the job id is the check run id), paginated, with the errors of D-67 (5xx and rate limits transient, 403 and 404 permanent). The diagnosis token already carries `checks: write`, which includes reading annotations.
3. One pure rule, in `core/logs/select.py`, shared by the use case and by the eval capture: a `cancelled` job counts as `timed_out` if and only if one of its annotations contains `has exceeded the maximum execution time` (case-insensitive). `select_failed_jobs` takes the annotations already read; everything else of D-58 stays. A promoted job flows through the normal path as a `timed_out` job, which makes GHA-TIMEOUT fire through its `on_conclusion` condition (the text is an annotation, never a log line).
4. For a `cancelled` run the use case decides **before it writes anything**: guard against a newer attempt, then list the jobs of the attempt and read the annotations of its cancelled jobs (earliest first, at most 10 jobs, stopping at the first timeout). Only if one is a timeout does the normal flow start (rows, check run, analysis, comment). If none is, the call returns and the job is `done`: no rows, check run, comment, checkpoint or budget reservation. A manual cancellation and a `concurrency: cancel-in-progress` stay silent. For `failure` and `timed_out` runs nothing changes: the check run is still created first.
5. Retries: the decision is recomputed (two cheap `GET`s), except when the analysis of the attempt is already stored, which proves an earlier call passed the gate. The jobs the gate listed go to the analysis, so a first call lists them once. The checkpoint, the publication guard and the locks work as in D-70. Attempt 2 of a run is decided on its own jobs and edits the check run and the comment of attempt 1; an attempt 2 that is a manual cancellation leaves attempt 1 as it was; an older attempt behind a newer published one returns before reading anything.
6. `publish_could_not_diagnose(..., only_if_started=True)`, which the handler uses for `cancelled` runs, writes only when this very attempt already started the check run. A transient or permanent error while reading the annotations (before any check run exists) therefore never turns a manual cancellation into a "Could not diagnose" check, even on the last attempt: the job fails or dies in the queue and nothing is published.
7. `evals capture` reads (with `gh api`, `GET`) the annotations of the cancelled jobs, redacts them, stores them in `annotations.json` (`{job id: [messages]}`) and applies rule 3; the eval runner loads that file and applies the same rule. A cancelled run without a timeout cannot be captured and `capture --all` skips it with a note. The lab manifest's evidence for `job-timeout` is the log line `The operation was canceled` (the timeout text is not in the log).
**Why:** The timeout is one of the 8 categories, and a job-level `timeout-minutes` is the usual way to hit it; with option (b) GHA-TIMEOUT never fires on a real run and the category rests on step timeouts alone. The cost is small and bounded: a job listing and at most 10 annotation calls per cancelled run, no log download, no LLM and no budget. Measured on the real API in the P1c spike (U7, 2026-11-16): the run of `job-timeout` ends `cancelled`, `GET /check-runs/{job_id}/annotations` answers 200 with two annotations of level `failure` (`The job has exceeded the maximum execution time of 1m0s` and `The operation was canceled.`), and a manual cancellation has neither the first one. The responses are in `tests/fixtures/github/jobs_job_timeout.json` and `annotations_job_timeout.json`.
**Alternatives:** (b) document the gap (zero work, but the `timeout` category is almost dead); read the log for `The operation was canceled.` (it cannot tell a timeout from a manual cancellation, and would publish a diagnosis on every cancelled run); create the check run first and delete it when the run was not a timeout (a check run cannot be deleted, and it would flash on every cancelled run); read the annotations of every job of the run (more calls for no gain: only cancelled jobs can be timeouts).
**Consequences:** Amends FR-04 and FR-06. A repository that cancels many runs by concurrency makes jobs that end without publishing (cheap, but visible in the queue; the dedupe key keeps one per attempt). A run that ends `failure` with a timed-out job next to a failed one is not examined for the timeout (its jobs are not looked at beyond D-58): the rule applies to `cancelled` runs only. A step-level `timeout-minutes` is unrelated: it ends `failure` and is diagnosed by GHA-CANCEL-TIMEOUT. A cancelled run whose annotations cannot be read (a permanent error such as a 404) never gets a check run and ends `failed` in the queue. The live check (a timeout is diagnosed, a manual cancellation publishes nothing) passed on 2026-11-17 (SPEC §13); its runbook is "Job timeouts (D-73)" in `lab/README.md`.

## D-74 — The handler of `diagnose_run` publishes "Could not diagnose" when the job gives up
**Origin:** Proposed
**Decision:** `worker/handlers.py::diagnose_run_handler` (not the `Worker`) runs the use case and owns what happens when a job gives up, so a check run never stays `in_progress` for ever:
- An invalid payload is a `PermanentError` (no run to report on, no retry).
- A `PermanentError` from the use case, or **any** error when `job.attempts >= job.max_attempts` (the attempt after which the worker marks the job `dead`, D-49), makes the handler complete the check run as "Could not diagnose" with a reason, and then **re-raise the original error** so the worker records `failed` or `dead`. Reasons: `LogsUnavailable` is `logs_expired`; a `PermanentError` with status 403 is `permission`; anything else is `internal_error`. A run with no failed job is not an error: the use case publishes `no_failed_job` itself and the job ends `done`.
- The publication is a best effort: `DiagnoseFailedRun.publish_could_not_diagnose` runs under its own 30 s timeout (`GIVE_UP_TIMEOUT_S`), whatever it raises is logged at WARNING and swallowed (a cancellation is not), so it never hides the error that got the job here. It respects the newer-attempt guard and takes the publication lock like every other write (D-70).
- A suspended or uninstalled installation (the token request is refused, `InstallationTokenRefused`, a `GitHubPermanentError` that is also an `InstallationUnavailable`, a new `PermanentError` in `core/errors.py`) publishes nothing: there is no token to write with.
- The use case (D-70) already publishes `permission` and `logs_expired` before it raises, because it knows the exact moment; the handler publishes the same reason again. That is idempotent and costs one `PATCH` on a rare path, and it is what makes the handler complete by itself for every other permanent error. For the same reason the use case now raises `LogsUnavailable` (with the status) instead of a plain `PermanentError` after a 410.
**Why:** A published check that says "analysing" for ever is worse than one that says "could not diagnose" (SPEC §8). The worker does not know what a check run is and no other job kind needs a hook, so the handler is the smallest place to do it.
**Alternatives:** an `on_give_up` callback in the `Worker` (more general, but the worker would learn about checks for the benefit of one job kind); publishing nothing (the check stays `in_progress`).
**Consequences:** A job that a lease sweep buries (the worker died during its last attempt, D-49) never reaches the handler, so its check stays `in_progress`; accepted, a sweep of stuck checks can come with the purge of P2. An `internal_error` on the last attempt can also overwrite a diagnosis that was already published if the last thing that failed was the comment; it is the price of not reading the store in the handler, and the comment keeps the full diagnosis. The wiring (`app/container.py`) is part of this step: the worker refuses to start without `GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY` (the message names the variables, never a value); `OpenAILLMClient` exists only with both `OPENAI_API_KEY` and `OPENAI_MODEL` (otherwise the pipeline publishes the rules diagnosis with `fallback_reason = disabled` and the log says which one is missing); the Postgres pools are sized for what holds a connection at once (the publication lock holds one for the whole GitHub call and `save_publication` inside it needs another, so `2 × WORKER_CONCURRENCY + 2` for the store). `GitHubReporter` gets the two P3 methods of the port as `NotImplementedError`, like the fake. The OpenAI SDK client is not closed explicitly at shutdown (the process ends). The end-to-end tests (`tests/e2e`) and the Postgres tests (`tests/integration/test_diagnose_*.py`) run the real GitHub adapters over `FakeGitHub`; nothing in this step talks to GitHub or OpenAI, so the live boxes of §13 stay open for the next step.

## D-75 — The `diagnose_run` payload carries the account; the use case makes sure the installation and repository rows exist
**Origin:** Proposed (completes D-54)
**Decision:** `DiagnoseRunPayload` gains `account_login` and `account_type` (from `repository.owner` of the webhook), optional because the jobs queued before P1c lack them. The use case `core/usecases/diagnose_failed_run.py` inserts the installation if it has no row (`InstallationStore.ensure_installation`: insert if missing, and never overwrites a row, so a stored `suspended_at` or `deleted_at` stays) and upserts the repository with `default_branch` and `private` from the payload. The plan was to do this after getting the installation token (proof that the repository is still installed); the credential belongs to the adapters, so the use case does it as the first thing it does, before any call to GitHub. A job without account whose installation has no row cannot be diagnosed: it ends as a `PermanentError` (nothing tracks that installation, and the run and failure rows need it).
**Why:** `repositories.installation_id` references `installations`, and `workflow_runs` and `failures` reference `repositories`, so the rows have to exist before the analysis is saved; D-54 left that to P1c. The webhook already has the account.
**Alternatives:** `GET /app/installations/{id}` with the app JWT (one more call per job for something the webhook already carries); requiring the `installation` event to have been seen (installations that predate P1a have no row).
**Consequences:** `InstallationStore` gets `ensure_installation` in the port, the in-memory and the Postgres adapters (`ON CONFLICT DO NOTHING`), and the contract suite covers both. `repository.owner` becomes a required field of the parsed `workflow_run` webhook. The use case (D-70, D-71) is tested on in-memory fakes only (`adapters/memory/github.py`: `FakeLogSource`, `FakeReporter`, `FakePullRequests`); nothing is wired to the worker until the next step of P1c.

## D-76 — The check run is named `CICD Doctor: <workflow>`, with the name sanitised and cut
**Origin:** Proposed (amends D-18)
**Decision:** The check run of a run is named `CICD Doctor: <workflow>`, built by `core/render.py::check_name(workflow_name)`:
1. the workflow name goes through `flatten` (secrets redacted, control, invisible and bidi characters removed, one line);
2. it is cut to 60 characters, with `…` when it was cut;
3. if nothing is left, the name is plain `CICD Doctor`.
The result is never longer than 80 characters. **No markdown escaping is applied:** the `name` field of the Checks API is plain text, and escaping would show backslashes. **U12 is confirmed (measured by hand on 2026-11-16):** the API stores a name with `<b>`, `**`, backticks and a link exactly as written, and the commit page of the GitHub interface shows it as **literal plain text**, without bold or link, so no escaping is needed (no `escape`, no test for it). Two check runs with the same name are created (201) and listed as distinct objects with their own `id` and `external_id`; the interface was not seen with two rows of one name (the test renamed the first one before looking), which is harmless because the identity is the `external_id`. In the summary of the commit, `neutral` check runs count as "passed" ("All checks have passed · 3 neutral checks").
- The **identity** of the check run is not its name but `external_id = "cicd-doctor:run:{run_id}"`, together with the id stored in `check_publications` (D-70). Renaming a workflow, or two names that collide after the cut, neither duplicates nor loses check runs: `start_check` updates the name on the `PATCH`.
- The sticky comment, which is one per (PR, workflow), carries the name in its heading, `### CICD Doctor: <workflow>`, written with `inline` (markdown-safe); the title of the diagnosis goes on the next line in bold.
- `flatten` now removes every Unicode format character (category `Cf`), not only the listed zero-width and bidi ranges: a property test found that a soft hyphen came through, and it is invisible in a name.
- `markdown_threats.py` lets through only the fixed prefix `### CICD Doctor: ` of the heading, so the workflow name after it is scanned like any other untrusted text.
- SPEC FR-11, §7.5 and the first P1c box of §13 say `CICD Doctor: <workflow>`; D-18 is amended.
**Why:** FR-11 said `CICD Doctor` for every check run, so two workflows failing on one commit gave two check runs that cannot be told apart in the list. The name of a workflow is written by whoever writes the repository (in a fork, an attacker), so it is untrusted text like everything else from the run.
**Alternatives:** a fixed `CICD Doctor` and the title as the only difference (the check runs stay indistinguishable in the list); the name uncut (GitHub documents no maximum, and a huge name breaks the interface); escaping markdown without knowing whether it is needed (visible backslashes if it is not).
**Consequences:** Whoever uses "required checks" by name cannot require `CICD Doctor` (the check run is `neutral` and never blocks a merge, D-18: expected). The workflow suffix is what tells the check runs of one commit apart in the list (U12 showed no grouping of check runs by name, and a `neutral` check counts as passed in the summary). A commit can carry many check runs of the same name from other apps (30 `run` in the lab), so the search is by `external_id` and app, never by name.
**Verified live (2026-11-16):** the name shows as literal plain text in the GitHub interface (measured by hand on a scratch commit), and the `app_id` filter of the check run listing works.

## D-77 — The embedding is computed in the use case before the LLM and travels in the checkpoint; an embedder error degrades, it never fails the job
**Origin:** Proposed (implements FR-08b; completes D-08 and D-69)
**Decision:**
- `DiagnoseFailedRun._analyze` embeds the `signature.text` of every job (the canonical, already redacted text of §7.4) in **one batch per attempt**, in a thread (`asyncio.to_thread`), right after `analyze_jobs` and before the LLM. The vector goes in `Failure.embedding` of the `RunAnalysisRecord` and is written with the rest of the checkpoint (D-69): no second write, no extra job. The model is symmetric (signature against signature), so the bge query prefix is not used.
- It runs before the LLM because the similar failures will enter the prompt (P2 d), and before the checkpoint because the embedding is part of what is computed once.
- **What is retried:** the same as before. A job that fails before the checkpoint embeds again (4 to 14 ms); one that fails after it never touches the embedder. An embedder error (`EmbedderUnavailable`, any other exception, or a result with the wrong shape) does **not** fail or retry the job: the failure is stored with `embedding = NULL`, the diagnosis goes on, and a WARNING names the class of the error and never a text (SPEC §8, row "Embedder"). `EMBEDDINGS_ENABLED=false` (no embedder at all) does the same on purpose. A failure without an embedding cannot be found by similarity later (there is no backfill).
**Why:** A model that cannot be downloaded must not stop the diagnoses (NFR-12): the rules, the LLM and the exact-signature cache work without it.
**Alternatives:** an `embed_failure` job after the checkpoint (the first prompt would have no similar failures, and a failure would exist for a while without a vector); embedding inside the Postgres adapter (the store would hide a model); failing the job when the embedder fails (a model that does not download would block every diagnosis).
**Consequences:** the checkpoint weighs 1.5 KB more per failure (384 × 4 bytes). `Failure.embedding` is used from now on. A model outage is silent for the user and visible in the log. Nothing reads the vector yet: `similar` on Postgres and its use in the flow are the next steps of P2.

## D-78 — The model lives in the worker process, warms up in the background, caches in a fixed directory and has a switch
**Origin:** Proposed (completes D-08)
**Decision:**
- `adapters/embeddings/fastembed_embedder.py::FastEmbedEmbedder` (`dims = 384`, model `BAAI/bge-small-en-v1.5`, served by fastembed from `Qdrant/bge-small-en-v1.5-onnx-Q`) lives in the **worker** process; the API never loads it. One shared instance, with a lock around the load and another around the inference; the use case calls it through `asyncio.to_thread`.
- **The warm-up is in the background and nothing waits for it.** `app/container.py::start_warming` starts a daemon thread that calls `warm()` before the consumers open, so the worker starts at once. Until the model is loaded, `embed` raises `EmbedderUnavailable` **immediately** (D-77). If the load fails, an ERROR is logged (class of the error only) and the next attempt is made by an `embed` call, in another background thread, at most every 5 minutes. A daemon thread, so a download in progress never delays a shutdown.
- `EMBEDDING_CACHE_DIR` (default `.cache/fastembed`, git-ignored) replaces fastembed's default of the system temporary directory, which Windows cleans. `EMBEDDING_THREADS` (empty = onnxruntime decides) limits the ONNX threads. `EMBEDDINGS_ENABLED=false` runs the worker without the model.
- **CI caches the model** (`actions/cache`, pinned by SHA, key = model name + SHA-256 of the `.onnx`, `51f1bd0a…49f2431`): a cold download took up to 166 s. A step downloads it on a cache miss, so the first run fills the cache; `tests/adapters/test_fastembed.py` fails in CI if the model is not in `.cache/fastembed`, and checks the SHA-256 of the file.
- **Measured in the P2 spike (2026-11-17):** loading with the file on disk takes 0.08 to 0.12 s; a cold download took 12 s once and 162 to 166 s the other three times (Hugging Face unauthenticated, variable), which is why blocking the start would be a gamble between one second and three minutes. RSS 430 to 480 MB with one instance (206 to 290 MB right after the load); a container capped at 512 MB survived with 482 MB, with no margin: the worker needs at least 1 GB for the model alone and, with the logs of D-71, should aim at 2 GB in P5. The image grows by about 200 MB (model 66.5 MB, fastembed and its dependencies 133 MB installed). Latency 4 to 14 ms per attempt (1 to 3 signatures), 17 ms p95 on Linux. Python 3.13 works on Windows (3.13.7) and Linux (3.13.18, `python:3.13-slim`), with identical vectors bit for bit between the two machines measured (a GitHub Actions runner later differed by up to 1e-4 per component, cosine still above 0.9999, so the test compares with cosine >= 0.9999 and 1e-3, not bit for bit): no need to pin 3.12.
**Why:** The model is what takes the memory and the time, and it is needed only where the diagnosis runs. The cache directory must survive a reboot and a temporary-directory cleanup, and CI must not depend on Hugging Face being fast.
**Alternatives:** an embedding service apart (another process, deploy and network hop; justified only if the worker RAM becomes a problem); loading the model per job (7.5 s per diagnosis); the model in the API (breaks NFR-01 on a cold start); blocking the start until the model is loaded (up to 3 minutes); OpenAI `text-embedding-3-small` (another network call and cost, D-08).
**Consequences:** during the first minutes after a cold start with no cached model, failures are stored without an embedding and are not searchable (D-77); P5 bakes the model into the image so a deployed worker never downloads it. `uv sync` adds fastembed, onnxruntime and about 30 packages. The checkbox of P0 about Python 3.13 and §14 are closed.

## D-79 — `similar`: an exact search per repository, no forks, an HNSW path behind a switch, and a pinned pgvector image
**Origin:** Proposed (implements D-10 for the vector search; D-77 and D-78 are the embedder)
**Decision:**
- **Filter (D-10).** `WHERE repository_id = :repo` is **inside** the query that orders the vectors, never applied after an approximate search. Failures of runs from a fork (`workflow_runs.is_fork`) and failures of the same `run_id` (a retry of the run is the same failure; the port gets `exclude_run_id`) are not similar failures. The diagnosis shown is the **published** one (`diagnoses.published`), not the newest row.
- **Query: exact by default.** The inner query reads `failures` only (`repository_id = :repo AND embedding IS NOT NULL`), orders by `(embedding <=> :q) + 0` and keeps `4 × k` rows. The `+ 0` makes the ordering something the HNSW index cannot serve, so the plan is the rows of one repository plus a top-N sort. The outer query joins the run and the published diagnosis, drops forks and the excluded run, keeps `1 − distance ≥ :min_score`, orders by distance and cuts at `k`. Score = `1 − cosine distance`, rounded to 3 decimals in `SimilarFailure` (so the same inputs print the same on every platform). Spike (C5, 2026-11-17): recall 1.0, 0.2–0.9 ms at 900 rows per repository, 5–13 ms at 20 000.
- **The `+ 0` trick was a hypothesis, and it holds** (`tests/integration/test_similarity_plan.py`, written and passing before anything depended on it). Measured on 2026-11-17 with pgvector 0.8.7 on PostgreSQL 17.11, 5 000 random 384-d rows analysed, one repository with 4 000 of them (80 %, well over the 15 % where the planner goes for the index) and two with 500, each failure with a published diagnosis (`EXPLAIN (ANALYZE)`, `k = 3`, so 12 rows read):
  - default query, big repository: `Seq Scan on failures f` (`Filter: embedding IS NOT NULL AND repository_id = 1`, 4 000 rows) → `Sort` with `top-N heapsort` → 12 rows; **2.0 ms; `failures_emb_idx` is not in the plan**;
  - default query, small repository: `Index Scan using failures_sig_idx` (`repository_id = 2`, 500 rows) → top-N sort; 0.25 ms; no HNSW either;
  - control, the same query ordered by `embedding <=> :q` with no `+ 0`: `Index Scan using failures_emb_idx` with the repository filter applied to what the index returns: **4 rows instead of 12 for the big repository, 2 for the small one** (38 candidates removed by the filter). That is the loss of rows and of recall the exact query avoids; the test asserts both plans, so it fails if the planner ever stops treating the two orderings differently;
  - the result of the default query equals a sequential scan with every index off (the top 5 ids, in the three repositories) and has `k` rows whenever the repository has them.
  Plan B (`SET LOCAL enable_indexscan = off`, then `WITH … MATERIALIZED`) was **not needed** and is not implemented. For a repository that holds most of the table the plan is a sequential scan, not the index or bitmap scan on `failures_sig_idx` that the spike measured at 20 000 rows: it is still exact and cheap at this size (2 ms for 4 000 rows), and it is what a planner should do when most of the table matches.
- **Opt-in HNSW path.** `SIMILAR_USE_HNSW` (default `false`) runs the same query without the `+ 0`, inside the transaction that does `SET LOCAL hnsw.iterative_scan = relaxed_order` and `SET LOCAL hnsw.ef_search = 100` (spike: recall 0.935 at 2.0 ms for a repository of 20 000 rows, against 0.848 without the iterative scan; never exact). It is meant for a repository above ~50 000 rows, which P2 does not expect. The adapter reads the extension version once and, below pgvector 0.8 (no iterative scans), logs a warning and keeps the exact search. `SET LOCAL` ends with the transaction: a test checks that the next user of the same pooled connection sees `hnsw.iterative_scan = off` and `hnsw.ef_search = 40`.
- **Why not HNSW by default.** With the repository filter and no iterative scan: 10.6 rows of 12 and recall 0.848 at 20 000 rows (0.842 with the default plan); with `relaxed_order` 0.908 (`ef_search` 40) and 0.935 (100): better, never exact. At the scale of NFR-11 (about 900 rows per repository) the planner already chose the exact plan.
- **Image pinned.** `pgvector/pgvector:pg17` is today PostgreSQL 17.11 with pgvector 0.8.7, and the tag **`0.8.7-pg17` exists**. `docker/compose.yaml` and `.github/workflows/ci.yml` use it, so development and CI do not move on their own. An existing local container keeps the old tag until `docker compose pull` and a recreate. RDS (P5) must offer pgvector 0.8.7 or later: **not verified**.
- **Index of `0001` kept** (`failures_emb_idx`, HNSW): unused by default; building it on 110 000 rows took 12 s with 256 MB of `maintenance_work_mem` and 43 s with the image default, it weighs 216 MB and costs 0.38 ms per insert. Dropping it is a SPEC §6 decision for P5 with real data. Migration `0004` adds nothing for the similarity search: the exact query reads one repository through the `repository_id` prefix of `failures_sig_idx`, and the same-run filter needs no `failures_run_idx` because `UNIQUE (run_id, run_attempt, job_id)` already starts with `run_id`.
- **Isolation.** `tests/contract/test_failure_store.py` runs on the memory and the Postgres adapters: the same vector in two repositories, a search that does not change when 1 000 closer rows belong to another repository, no forks, no same run, published diagnosis only, no failure without an embedding. That is the evidence for the isolation box of §13 P2 (a property of the store, not of the whole flow).
**Why:** A vector index that filters afterwards makes a big repository drown a small one and breaks isolation by *recall*, even when content never leaks. The exact search over one repository is cheap at this size and has no recall to tune.
**Alternatives:** filtering after a global search; one partial HNSW index per repository (does not scale to 100 installations with many repositories); one table per repository (a migration per repository); putting the score and the fork filter inside the inner query (the planner stops using the index once joins are there).
**Consequences:** `SimilarFailure` carries `run_id` and `run_attempt` instead of `run_url` (the renderer builds the link: no stored string is trusted as a URL, D-64; SPEC §5). The port gains `exclude_run_id`. A repository with many fork failures can return fewer than `k` rows (the inner query reads `4 × k`). A failure of a fork is never shown as similar to another (consistent with D-17). Nothing calls `similar` yet: the flow is wired in later steps of P2.

## D-80 — `SIMILAR_MIN_SCORE` stays at 0.85
**Origin:** Interview (the author, 2026-11-17: 0.85 is not changed; if a later measurement asks for another value the author is told **before** the box of §13 or the default is edited)
**Decision:** `SIMILAR_MIN_SCORE = 0.85` stays. It was a hypothesis, so the spike measured it with the quantised model that fastembed serves (`Qdrant/bge-small-en-v1.5-onnx-Q`, D-08): 11 positive pairs (the lowest 0.905, median 0.974) and 253 negative pairs (28 of the same category, maximum 0.842; 225 of another, maximum 0.873, p99 0.785, median 0.633). At ≥ 0.85: recall 11/11, one false positive (`tf-init ~ tf-validate`, two Terraform failures), precision 0.92; at ≥ 0.88: no false positive. The hypothesis of a high floor of similarity between unrelated texts did not hold. It is not raised to 0.88: the positives are 11 pairs made by editing real logs, and tuning to them would overfit; it is re-calibrated with the public cases of P4. The twins of the lab score 1.0.
- **Procedure (repeat it when the model or the signature normalisation changes):** (1) vectors of the 25 lab signatures with the quantised model; (2) the cosine matrix; (3) about 30 labelled pairs (the same failure in another module, host, port or test; the same error under another workflow name; same-category negatives with another cause); (4) the histogram of both groups and the threshold that gives precision ≥ 0.9 with the highest recall. The vectors and the pairs are kept as fixtures with a test that reproduces the measurement without downloading the model, so changing the model forces a new calibration (see the embedder, D-77 and D-78).
**Why:** The score decides what a user is told is "the same failure". A wrong threshold either hides real repeats or puts a stranger's fix in the report.
**Alternatives:** leave 0.85 unmeasured; calibrate with the LLM as a judge (no key, and it measures something else); a threshold per category (23 cases are too few).
**Consequences:** The sample is small: it fixes the order of magnitude, not the tail. Windows and Linux give identical vectors (difference 0.0 with the same versions), so the fixture test can use a tolerance of 1e-6 and notice a change of fastembed or onnxruntime. `SIMILAR_MIN_SCORE` remains configuration, in case real traffic asks for another value. Nothing changes in this step: the value is only used from the step that wires `similar` into the flow.

## D-81 — The exact-signature cache: only verified LLM answers of the current prompt, never from a fork, with their origin
**Origin:** Proposed (completes the cache of FR-15 and §9.4)
**Decision (store side, `p2-similar-store`):**
- `cached_diagnosis(repo_id, signature_hash, max_age, prompt_version)` returns `CachedDiagnosis(diagnosis, origin_id)` or `None`. The key is `(repository_id, signature_hash)` and the row must be `source = 'llm'`, within `max_age`, `evidence_verified`, made with the **current** `prompt_version` and belong to a run that is **not** a fork. A `cache` row is never served again (the TTL runs from the real call, so no chain of copies extends it) and neither is a `rules` row.
- A new prompt version invalidates the cache (an answer to another prompt is not comparable); a new model does not.
- **Forks (D-17):** a fork controls its logs, and `/cicd-doctor explain` (P3) can produce an `llm` row from them; the `is_fork = false` filter keeps that answer out of the cache of normal runs from now on. The order of §9.4 (fork before cache) is unchanged: a fork run is not served from the cache either.
- `origin_id` is the `diagnoses.id` of the row served; it is what fills `cached_from` (a column that exists since `0001`; the flow writes it, see below).
- Migration `0004` adds `diagnoses_cache_idx ON diagnoses (failure_id) WHERE source = 'llm'`: the lookup joins failures and then reads only `llm` rows, which are a minority of the table.
**Decision (flow side, `p2-cache`):**
- **Where.** `DiagnoseFailedRun._diagnose_all` asks the store for each job, **only when the policy without a cache would say `llm`** (`decide` with `cache_hit = False`): a fork, `llm: off`, `on_demand` and `LLM_ENABLED=false` never read the database. `CACHE_TTL_DAYS=0` turns the cache off. The call is `cached_diagnosis(repo_id, signature.hash, CACHE_TTL_DAYS, PROMPT_VERSION)`; a database error is a `TransientError` like any other query (the job retries, nothing is swallowed). The order of §9.4 does not change: a fork run is not served from the cache (a fork controls its logs, and this keeps its answers out in both directions).
- **Re-anchoring (`core/diagnosis/cache.py::reuse_cached`).** The stored evidence was verified against another run's excerpt, and the same signature does not mean the same line numbers. Each cited text is searched **by text** in the new excerpt with the comparator of `finalize` (`normalise_quote` / `quotes_line` in `validate.py`: no copied `L<n>:` prefix, whitespace collapsed, also the escaped form shown in the prompt), on the line closest to the old number among those not used yet, and is renumbered. A quote that is gone is dropped. `suggestion` is removed (its path and lines belong to another commit). `source` becomes `cache`; the texts, `category`, `model`, `prompt_version` and `confidence` are kept. If **no** evidence is left the function returns `None`, which is a cache miss: the normal policy goes on and calls the model. What is published is therefore always a quote of the log that is shown.
- **Budget.** A hit is decided before the reservation, so it reserves nothing and `llm_usage.calls` does not move; the stored row has no usage (`StoredFailure` forbids it for `cache`).
- **Origin.** `StoredFailure.cached_from` is the `diagnoses.id` served (`CachedDiagnosis.origin_id`); both adapters store it in `diagnoses.cached_from` (a column of `0001`, no migration). When a record is read back the adapter also fills `StoredFailure.cached_from_run_id` (the `run_id` of the origin's failure: a join in Postgres, a lookup in memory), which `DiagnosisReport.cached_from_runs` carries to the renderer: the source line says `cached from run #N` with `N` an integer built by the renderer (D-64), or `cached from an earlier run` when the origin is unknown (its row was deleted: `ON DELETE SET NULL`).
- **Matrix limit.** The jobs of one attempt are looked up at the same time and nothing of the attempt is stored until the checkpoint, so three jobs of a matrix with the same signature each call the model (three reservations); the cache serves the next run. A test documents it. Serving siblings would need the first answer before the others start (serialising the jobs or a second pass after the first LLM answer): not done, it costs one call per extra job and only on the first failure.
- **No key.** A hit does not need the OpenAI client: with `OPENAI_API_KEY` unset a stored answer can still be served until it expires. It is what the policy says (the `cache` branch sits before the "no client" fallback) and it costs nothing.
**Why:** Without these filters an answer to an older prompt, an unverified one, or one written about an attacker's log could be published as the diagnosis of a normal run.
**Alternatives:** the key by hash alone (ignores a prompt change); serving the cache to forks (more saving, but it changes §9.4 and D-17; left as an improvement with data).
**Consequences:** The port changes (SPEC §4.3). `DiagnoseConfig` gains `cache_ttl_days`, `StoredFailure` gains `cached_from` and `cached_from_run_id`, `DiagnosisContext.cached` is a `CachedDiagnosis` and `JobDiagnosis` carries `cached_from`. §13 P2 box 2 ("an identical lab failure within 7 days → source `cache` and `llm_usage.calls` unchanged") is covered offline only (memory, Postgres and the end-to-end test); the live part needs a real `llm` diagnosis, so it stays open until it is measured with the OpenAI key.

## D-82 — Similar past failures reach the prompt as untrusted data and the report as ids; prompt `diag-v2`
**Origin:** Proposed (plan P2 (d); the author closed: forks stay out of what `similar` returns, the comment shows one line and the check run up to three, `SIMILAR_MIN_SCORE` stays at 0.85, D-80)
**Decision:**
- **Where.** `DiagnoseFailedRun` asks `store.similar(repo_id, embedding, SIMILAR_K, SIMILAR_MIN_SCORE, exclude_run_id=run_id)` for each job **before** the model (in `_diagnose_all`, from the embedding of the batch of D-77) and puts the result in `DiagnosisContext.similar`, which the pipeline copies to `DiagnosisRequest.similar`. A job with no embedding (the embedder was not loaded) has `similar = []` and no query is made; `SIMILAR_K=0` turns the whole feature off. A database error propagates, so the job retries like any other query. The result is **not** part of the checkpoint: when the report is built (also on a retry after the checkpoint) the same query runs again from the stored embedding. It is context, not a diagnosis; a retry may list a newer neighbour.
- **Forks.** The policy of §9.4 is unchanged: a fork run never calls the model, so its `similar` never reaches a prompt, but it is shown in its report (the history it reads is the non-fork one: `similar` never returns a failure of a fork, D-79). Nothing a fork writes can end up in another run's prompt or report.
- **Prompt `diag-v2`.** `PROMPT_VERSION` and `CANARY` move to `diag-v2`, so the exact-signature cache of D-81 (keyed on the prompt version) and the eval answer cache start empty on purpose. The system prompt describes `<untrusted_similar nonce="N">` as data derived from earlier logs, as untrusted as the log: background only, never an instruction and never a source of evidence (evidence is quoted from the excerpt). The block uses the same per-call nonce and the same escaping of both tag names. Each line is `- run <int>, score <x.xx>, <category>: <cause cut to 200 characters>[, fixed by <sha>]`: the cause goes through `_meta` (controls removed, one line, tag names escaped), the run id is an integer and the commit is written only when it matches `^[0-9a-f]{7,40}$`, else it is left out.
- **Report.** `DiagnosisReport.similar` is filled for every job that has some. `render._similar` builds `[run #N](https://github.com/<repo>/actions/runs/<N>)` from integers and a repository name that matches the same pattern as the step link (without a valid name the run is plain text, with no link), puts the cause through `inline` and the commit through `code` (same `^[0-9a-f]{7,40}$` rule). The check run lists up to **three** items under "Similar past failures"; the sticky comment one line, "Similar past failure" (the best one): the comment is the short version (§7.5).
- **Oracle.** `evals/runner/markdown_threats.py` lets through exactly that link and the line prefix of a similar item, and nothing else: any other link, image, mention or markup in a cause is still a finding.
- **Tests.** Second-order injection: a poisoned list (instructions, closing tags of both blocks, mentions, links, images, HTML, bidi, markdown in the commit) goes through `build_messages`, `render_check` and `render_comment`, also with an obedient model, and the oracle of D-64 finds nothing; isolation by repository; a fork shows similar failures and the model is not called (counter); end to end and on Postgres with `FakeEmbedder`.
**Why:** What an earlier log said is attacker-controlled when the log was. Treating a stored cause as untrusted in the prompt and as untrusted prose in the markdown keeps a second-order injection as inert as a direct one (D-25, D-64), and building the link from integers means no stored string is ever a URL.
**Alternatives:** storing a URL in `SimilarFailure` (a stored string would be trusted as one); showing three items in the comment too (a long comment, and the check run has them); keeping `diag-v1` (the prompt changed, so answers to the old one must not be served as current); saving `similar` in the checkpoint (a new column or table for something that is cheap to recompute and can only go stale).
**Consequences:** SPEC §5, §7.5 and §9.3 change. The reservation of the budget still estimates from the excerpt only; the block adds at most `SIMILAR_K × ~80` tokens, and the settlement uses the real usage. §13 P2 box 3 ("a similar but not identical failure → the report lists the earlier run (score ≥ 0.85) with its fix commit if any") is covered **offline only** (unit, end to end and Postgres with `FakeEmbedder`, whose scores are not the model's); the live measure with the real embedder and a real `llm` diagnosis is step (h). The "fixed by" text appears only when a later step stores a fix commit; it is tested with a stored value.

## D-83 — The success path: which failures a success resolves, and why no arrival order loses a resolution
**Origin:** Interview (the author, 2026-11-17: a success in **another** run with the **same** `head_sha` resolves nothing and the failure stays open; an empty `head_branch` can only resolve as flaky, never by commit). Rules and store in `p2-outcomes-core`; the flow (webhook, `record_outcome`, late resolution in the diagnosis) in `p2-outcomes-resolve`. The comment is rewritten in `p2-outcomes-comment` (D-84).
**Decision (rules and store):**
- **Order of runs:** `(run_id, run_attempt)`, the key that already orders publications (D-69). GitHub run ids grow with creation (to be confirmed with the real data of the live check of P2); the worker's clock is never used.
- **`core/outcomes.py`** (pure, no I/O): `decide(outcome, open_failures)` and `decide_late(failure, later_successes)` apply one rule, `is_stale(created_at, now, after=30 d)` is the age check of D-85. A failure is resolved only if it is `open`, of the same repository, workflow and branch key as the success (the branch key includes the head repository, so `alice:main`, `bob:main` and the base `main` never mix) and **before** the success by `(run_id, run_attempt)`:
  - same `run_id` (an earlier attempt): `resolved_flaky` (same commit by definition, D-24);
  - another `run_id`, another `head_sha`, `head_branch` not empty (on both sides): `resolved_by_commit`, `fix_commit_sha` = the success's `head_sha`;
  - **nothing:** another run with the same `head_sha`; an empty `head_branch`; a failure of a later run than the success; anything not `open`.
- **Store.** `record_success(run, attempt)` inserts the `workflow_runs` row only if there is none (`ON CONFLICT DO NOTHING`) and the `run_attempts` row as `success` (idempotent, never rewritten), because `save_run` would overwrite `pr_number` with `NULL`: the webhook of a fork run has no pull request (D-72) and a diagnosis may have found it. `later_successes(repo, workflow, branch_key, after_run_id, after_attempt)` returns `SuccessRef(run_id, attempt, head_sha)` ordered by `(run_id, attempt)`. `resolve(..., resolved_by)` stores the success and `resolved_at`, all or nothing. `resolved_by(repo_id, run_id, attempt)` returns what that success resolved. It takes `repo_id`, which the plan did not list: every lookup is scoped by repository (D-10).
- **Races (the property the flow relies on).** The success is written **before** it reads the open failures, and the failure (the checkpoint) is written **before** the diagnosis reads `later_successes`. Under READ COMMITTED, for every interleaving of those four operations at least one side sees the other, and if both do, `resolve` changes only what is `open`. `tests/integration/test_outcome_race.py` runs the six interleavings and a concurrent run with two engines on the real database: in all the failure ends resolved.
- **Known limit.** The final *status* can depend on arrival order only when two successes could resolve the same failure differently (a re-run of the same run, which would make it flaky, and a later run on another commit, which would make it fixed) and the later run arrives **first**: the failure is then `resolved_by_commit` and the re-run, which came earlier in run order, finds it already resolved. Whether it is resolved never depends on the order (a test with random permutations); `decide_late` takes the earliest success, which is the order of arrival of the webhooks in all but a delayed delivery.
- **Index.** No index for `open_failures` by branch. Measured on a repository with 90 000 runs and 1 777 open failures on one branch, the query takes about 10 ms (a scan of `workflow_runs`, because `head_repo || ':' || head_branch` cannot use `workflow_runs_branch_idx`); `tests/integration/test_outcomes_plan.py` keeps the plan of the three queries under watch. `open_failures` loads whole excerpts: a light variant of ids is the answer if a branch that is always red makes it heavy.
**Decision (flow, `p2-outcomes-resolve`):**
- **Webhook.** `workflow_run.completed` with `conclusion = success` becomes a `record_outcome` job with a `RecordOutcomePayload` (`RunRef`, `completed_at`, `default_branch`, `private` and the account, parsed by the same `_WorkflowRunEvent` as a failure) and the dedupe key `outcome:{run_id}:{run_attempt}`. `skipped`, `neutral`, `action_required` and `stale` stay 204. The webhook still never touches the database (NFR-01).
- **`RecordRunOutcome`:** ensure the installation and repository (`core/usecases/rows.py`, shared with the diagnosis, D-75); `record_success` first; `open_failures` then `decide`; `resolve(..., resolved_by=(run, attempt))` once per status and fix commit. No GitHub call and no token: a failed job is retried by the worker and never publishes "Could not diagnose" (a success has no check run to close); an invalid payload is a `PermanentError`.
- **Late failure.** After the analysis is stored or read and before the report, `DiagnoseFailedRun` asks `later_successes` once per attempt (its failures share workflow and branch), applies `decide_late` and `resolve`s, then reads the record again, so the check run and the comment are built from the resolved state and no "Likely cause" is published on a branch that is already green. `Resolution` carries `resolved_by` (the success) so the late path stores the same provenance as the success path.
**Why:** Without the order bound a slow run that ends green after a newer push failed would close a failure of a branch that is still red; without a store method of its own a success would erase the `pr_number`; without both writes before both reads a late failure would stay `open` for 30 days under a comment that says it is red.
**Alternatives:** resolve in the webhook (a database in the request path, NFR-01); compare commit trees to find empty commits (more GitHub calls, D-24 discarded it); treat the same `head_sha` in another run as flaky (a `push` and a `pull_request` of one sha do not run the same code: it would state something false); order by `completed_at` (the clocks of GitHub and the worker are not one source); let a resolution be overwritten by an earlier success (would make the status independent of order, at the price of an UPDATE guarded by the run order; left out because the case is a delayed delivery).
**Consequences:** SPEC §2 (FR-04, FR-17), §4.3, §5.2 (new models `SuccessRef`, `ResolvedBy`, `RecordOutcomePayload`), §6 (migration `0006`) and §7.2 change. `FailureStore` gains `record_success`, `later_successes` and `resolved_by`, and `resolve` a `resolved_by` parameter that defaults to `None`. The success path is wired: the webhook, the worker (`record_outcome_handler`, with a store of its own) and the late resolution. The report and the comment do not change yet: they still show the failure as open until D-84 is finished. §13 P2 boxes 5 and 6 stay open (only the offline part, with the comment, is covered by the next step; the live check is step (h)).

## D-84 — A resolution remembers which success made it, and only the comment says it is resolved
**Origin:** Interview (the author, 2026-11-17: when a failure is resolved only the **comment** is edited; the check run stays as the record of the diagnosis). This entry holds the provenance, done in `p2-outcomes-core` and used by `p2-outcomes-resolve` (both the success path and the late path store it); the rewrite of the comment, below, is `p2-outcomes-comment`.
**Decision (provenance):** `failures.resolved_by_run_id bigint` and `resolved_by_attempt int` (migration `0006`, both `NULL` while the failure is `open` or `stale`), and the partial index `failures_resolved_by_idx (resolved_by_run_id, resolved_by_attempt) WHERE resolved_by_run_id IS NOT NULL`. A retry of `record_outcome` reads `resolved_by(repo_id, run_id, attempt)` to find what its success already resolved, so it can rewrite the comment even when the first try failed after `resolve` and `open_failures` no longer returns anything. No foreign key: the success may be purged (D-85) before the failure. `resolve(..., resolved_by=None)` clears it, like `resolved_at` for `open`.
**Decision (the comment, `p2-outcomes-comment`):**
- **What changes.** Only the sticky comment. The check run keeps the diagnosis as it was published and a resolution never calls the Checks API. The banner replaces the bold title: `**✅ Passed on re-run without code changes — recorded as flaky**` or `**✅ Fixed by `abc1234`**`, with the compact diagnosis below. A check run written by a late diagnosis (the failure came after its success) carries the same banner as its title (`✅ Fixed by abc1234`, plain text) and its first line, because it is published once, already resolved.
- **The banner is built, not copied.** `render.resolution_banner` uses the status and a `fix_commit_sha` that must match `^[0-9a-f]{40}$` (shown as seven characters). With any other value the text is `✅ Fixed by a later commit`, with no sha and no link. The link `[Compare the changes](https://github.com/<repo>/compare/<failing sha>...<fix sha>)` is derived from the repository name (checked like the one of the step link) and the two shas (both 40 hex): nothing from a log or a stored string is ever a URL (D-64). The banner appears only when **all** the failures of the report were resolved the same way; a mix, an open one or a `stale` one keeps the "Likely cause" title. The oracle of the injection suite (`evals/runner/markdown_threats.py`) accepts exactly that link and nothing else new.
- **Step 4 of `RecordRunOutcome`.** The failures come from `resolved_by(run, attempt)` (so a retry finds what the first try already resolved), grouped by comment key `(repo, pull request, workflow)`; the pull request is the one the diagnosis stored in `workflow_runs.pr_number` (the success looks up nothing). Under the `publication_lock` of the key: no publication (nothing was ever commented) means nothing to do; if the publication shows an attempt that is not one this success resolved (a later run already wrote it) the comment is **not touched**; otherwise the analysis of that attempt is read again, the report is rebuilt (`core/usecases/report.py`, shared with the diagnosis) and the comment is updated by id. The publication is saved as `(success run, success attempt)` under the usual newer-wins guard, so a retry finds a publication that is no longer one of the failures' attempts and does nothing more, and a later failure (a larger run id) still writes its own comment normally.
- **The late diagnosis reads again under the lock.** A success that is recorded after the diagnosis read `later_successes` and before it publishes finds no comment, so there is nothing for it to rewrite. `DiagnoseFailedRun._comment` therefore reads the analysis again under the comment lock and rebuilds the report when a status changed. Either the success rewrites the published comment, or the diagnosis writes it already resolved (a test forces the interleaving on Postgres). The check run is not recomputed: it keeps the diagnosis as the record.
- **Wiring.** `RecordRunOutcome` shares the HTTP client, the app authentication, the reporter and the store with `DiagnoseFailedRun` (`worker_use_cases` in the container); the token has the same permissions (`actions: read, checks: write, pull_requests: write`): no new one, and no `contents: read`.
**Why:** `resolve` is one transaction and the comment is a GitHub call after it; without the provenance a crash between them leaves the comment red for good.
**Alternatives:** a table of resolutions (a join for one pair of numbers); deriving it from `fix_commit_sha` (flaky has none); a retry that scans all `resolved_*` failures of the branch (no key, and it would rewrite comments of other successes).
**Consequences:** SPEC §2 (FR-17), §6, §7.2, §7.5 and §8 change. §13 P2 boxes 5 (flaky) and 6 (fix flow) are covered **offline** (unit, end to end on `FakeGitHub` and Postgres); they are ticked only with the live check of step (h) (the lab: fail, re-run, and a fixing commit).

## D-85 — The purge is a daily job with a date dedupe key; it is idempotent by construction and works in batches
**Origin:** Interview (the author, 2026-11-17): FR-18 is widened with `stale` and with finished jobs; the purge runs at 03:00 UTC; the data of an uninstalled installation is purged after 7 days of grace. Completes D-34.
**Decision:**
- **Schedule.** `worker/scheduler.py::Scheduler` runs as one task per worker process next to the `Worker`, stopped by the same `stop` event (a `TaskGroup`: if one crashes the other stops). Every 15 minutes it calls `enqueue(NewJob(PURGE, dedupe_key="purge:<UTC date>", run_after=<that date> at PURGE_HOUR_UTC))`. `UNIQUE (dedupe_key)` with `ON CONFLICT DO NOTHING` makes exactly one of N workers create the job of the day, and the fenced lease (D-47) makes one run it. No elector, no extra lock. A failing queue is logged and tried again on the next tick (D-49). `PURGE_ENABLED=false` starts no scheduler.
- **What it does** (`core/usecases/purge.py::run_purge`, every cut from the injected `Clock`, D-48): (1) `FailureStore.mark_stale`: `open` failures created more than `STALE_AFTER_DAYS` (30) ago become `stale` (with `resolved_at`); a resolved status never changes. (2) `JobQueue.purge`: deliveries older than `RETENTION_DELIVERIES_DAYS` (30) and `done`/`failed`/`dead` jobs not updated for `RETENTION_JOBS_DAYS` (30), whose payloads carry repository data; queued and running jobs are never touched. (3) `FailureStore.purge`: run attempts with `completed_at` older than `RETENTION_FAILURES_DAYS` (90) with their failures and diagnoses, then the runs left without attempts, then publication records not updated for 90 days. (4) `InstallationStore.gone_repositories(now - UNINSTALL_GRACE_DAYS)` and `FailureStore.purge_repositories`: everything of the repositories whose `removed_at`, or whose installation's `deleted_at`, is older than 7 days. `track_installation` clears both on a reinstall, so inside the grace nothing is deleted. **Not purged:** `llm_usage` (spend accounting), `installations` and `repositories` (soft delete, D-54).
- **Batches.** Batches of 1 000 rows, one short transaction each, rows taken with `FOR UPDATE SKIP LOCKED`; a short batch ends the loop. Diagnoses, failures and attempts are deleted explicitly (not by the schema cascade) so each can be counted. The log carries counts only.
- **Lock order (found by the test with two engines).** `purge` and `purge_repositories` both start from `run_attempts` (`SKIP LOCKED`) and delete what hangs from those attempts. A first `purge_repositories` that started from `workflow_runs` ended in `DeadlockDetected` next to `purge`. A run is removed after its attempts, driven by the runs of the deleted attempts (no scan of `workflow_runs`); whichever concurrent purge commits last removes it.
- **Idempotency.** Every statement is a time predicate: a second run finds nothing.
- **Migration `0005`** (the plan said `0006`; `0005` was the next free number in `main`): indexes on `webhook_deliveries (received_at)`, `jobs (updated_at)` for finished jobs, `run_attempts (completed_at)`, `failures (created_at)` for open ones, `check_publications` and `comment_publications (updated_at)`.
- **Settings:** `PURGE_ENABLED` (true), `PURGE_HOUR_UTC` (3), `RETENTION_DELIVERIES_DAYS` (30), `RETENTION_FAILURES_DAYS` (90), `RETENTION_JOBS_DAYS` (30), `STALE_AFTER_DAYS` (30), `UNINSTALL_GRACE_DAYS` (7).
**Why:** D-34 had no job, `stale` (D-24) had nothing that set it, and finished jobs kept repository data for ever. A date in the dedupe key gives "once a day across N workers" from the queue we already have.
**Alternatives:** `pg_cron` (not in the Compose image, tests would not see it); EventBridge plus an ECS task (right for P5; the `purge` job is the target, so P5 can replace the scheduler); a "last purged" row with an advisory lock (re-implements the dedupe); one `DELETE` per table (long locks); relying on `ON DELETE CASCADE` (cannot count).
**Consequences:** SPEC §2, §4.4, §4.5, §6, §7.2 and §13 change; `JobKind.PURGE` has a real handler; the ports gain `JobQueue.purge`, `FailureStore.mark_stale/purge/purge_repositories` and `InstallationStore.gone_repositories`. A worker off all day does not purge that day. A repository removed long ago costs one empty batch per table every day. The comment of a `stale` failure is not rewritten.

## D-86 — The eval variant `llm+hints+similar` runs over an in-memory history that follows `captured_at`
**Origin:** Plan of P2, step (g). Closes the eval side of FR-16 and D-81/D-82 (the author, 2026-11-17: `SIMILAR_MIN_SCORE` stays at 0.85).
**Decision:**
- **The variant.** `llm+hints+similar` is a model column like the other LLM variants (`llm+hints+similar [model]`). It runs the same `DiagnoseJob` as `llm+hints`, with `DiagnosisContext.cached` and `.similar` filled the way `DiagnoseFailedRun` fills them: `FailureStore.cached_diagnosis` (TTL `CACHE_TTL_DAYS`, current prompt version, verified LLM answers, no forks) plus `reuse_cached` to re-anchor the evidence, and `FailureStore.similar` with `SIMILAR_K` and `SIMILAR_MIN_SCORE`, all read from the defaults of `DiagnoseConfig`, so the eval cannot drift from the app. The lookup is the only part that is written twice (the use case reads it from its own state, the eval from `evals/runner/history.py`); the policy is not copied: the eval runs non-fork, enabled, `auto`, where the policy always says `llm` unless a cache hit exists.
- **The history** (`evals/runner/history.py`). One `InMemoryFailureStore` per `repo` of the cases. The cases are processed per repository by `captured_at` (a case without one goes after the dated ones, by id), a fake clock moves to each `captured_at` (never backwards) so the cache TTL means what it means in production, and after each case its published diagnosis, its excerpt, its signature and its embedding are stored as a checkpoint. A case is stored **after** it is diagnosed, so it never finds itself, and the run id of the case is passed as `exclude_run_id`, so two jobs of one run do not see each other (as in production). `source = injection` cases have no history: nothing is looked up for them and nothing of them is kept, so an attack can neither be served from the cache nor poison a later case. The results come back in the order of the cases, not in the processing order.
- **Embeddings.** The CLI uses the production `FastEmbedEmbedder` (the model loads before the run; if it does not load the run is an error and not a silent run without similarity). Library callers pass an `Embedder` (`FakeEmbedder` in the unit tests); the variant refuses to run without one.
- **Why the score rounding is kept.** `SimilarFailure.score` is rounded to 3 decimals (D-82), and the score goes into the prompt, so it is part of the answer cache key of D-65. Measured in the spike (B3): the vectors of the 36 lab signatures are **bit for bit identical** between Windows and Linux (0 of 666 pairs differ at 2 and at 3 decimals; maximum difference 0.0, same onnxruntime and model hash), and identical between processes and batch sizes. So the rounding is a guard against a version change, not a need today; the remaining risk is a score that sits on a rounding edge after such a change, and it would show as a cache miss (a new live call), never as a wrong answer.
- **Metrics.** The report has `Cache hits` and `Similar hits` columns (count and share over the cases with a history) for this variant (`n/a` for the others), and a section "Effect of the memory" with, per model, `llm+hints+similar` minus `llm+hints`: accuracy in percentage points, total cost, cache hits and similar hits. The JSON has the same in `memory_effect`. The section carries a small-n warning: the lab has 23 distinct failures, so the effect shows almost only on the two repeated captures (py-assert, py-modnf); the public cases of P4 are what will give signal.
- **Costs and the answer cache.** The variant calls the model only for cases that miss the history cache, and its requests carry the similar block, so they are other cache keys than `llm+hints`: the first `--live` run fills them. Nothing was run live in this step: the answer cache of the evals is empty and the real numbers wait for the OpenAI key.
**Why:** Production behaviour depends on what earlier runs left behind; a stateless variant could not measure it, and a shared store across the whole lab would let a case see the future. Using the production store class and constants keeps the numbers about the app, not about a model of it.
**Alternatives:** a Postgres store in the eval (needs a database to run a report, against D-65's "offline and in memory"); a precomputed neighbour table (it would not exercise the cache, the TTL or the prompt); one history for all repositories (D-10 isolates them); not rounding the scores (a different key on each platform if vectors ever differ); falling back to `FakeEmbedder` when the model is missing (similarity numbers that mean nothing).
**Consequences:** SPEC §11.2, §11.3 and §13 (P2, box 8) change. `CaseResult` gains `similar_found` and `similar_top_score`, `Metrics` gains four history fields, `run_evals` takes an `embedder`, and `LiveSettings` reads `EMBEDDING_CACHE_DIR` and `EMBEDDING_THREADS`. A run of this variant on a machine without the cached model downloads it (66 MB, D-78).

## D-87 — The worker silences every HTTP client logger, not only the ones of its own adapters
**Origin:** The live verification of P2 (2026-11-18): the worker printed `httpx HTTP Request: GET https://huggingface.co/...` at INFO while the embedding model downloaded. The author asked for the fix.
**Decision:** `HTTP_LOGGERS` in `app/container.py` adds `httpx`, `httpcore` and `huggingface_hub` to `httpx2` and `httpcore2`, and `configure_logging()` sets them all to WARNING (their children inherit it). The first pair is the client that `huggingface_hub`, used by fastembed, runs on; the SDKs of the adapters run on `httpx2` (D-63, D-67).
**Why:** At INFO these libraries print the URL of every request. The URLs of the model are public and carry no credential, so nothing leaked. But the rule of D-67 is that a request URL never reaches a log (a log blob URL carries its signature), and a rule per library that happens to be used today breaks the day a new dependency makes a request with a signed URL. A test fixes the list.
**Alternatives:** leave it (it holds today; one dependency change from a leak); set the root logger to WARNING (hides the worker's own INFO lines, which the runbooks read); filter by message (fragile).
**Consequences:** the download of the model shows no request lines; the progress bars of `huggingface_hub` (stderr, not logging) are unchanged, and a failed download is still reported by the embedder (D-78). No SPEC change.
