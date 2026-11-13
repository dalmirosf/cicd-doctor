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
| D-18 | Output: neutral check run + one sticky comment per (PR, workflow) | Interview |
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
**Decision:** Always create a `CICD Doctor` check run on the run's `head_sha` (`in_progress` at start, `completed` with conclusion `neutral`). When the run has a PR, also create one comment per (PR, workflow), identified by a hidden marker and by its stored id, and edit it on re-runs, new failing pushes, and resolution. Comments can be turned off per repo (`comment: false`, P3).
**Why:** The check run is visible for every run, including pushes to `main` without a PR, and holds the full report. The comment brings the conclusion to where reviewers look. `neutral` ensures the doctor never blocks merges or adds a second red cross to a failure that is already red. One comment per workflow keeps parallel workflows from overwriting each other, while still avoiding duplicates.
**Alternatives:** Check run only (less visible); comment only (no output for runs without a PR); one comment per PR covering all workflows (concurrent updates from different workflows would conflict).
**Consequences:** A PR with several failing workflows gets several comments, one per workflow, each updated in place.

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
**Amended by D-43 and D-44:** import-linter is not used (a pytest test replaces it), and the FastAPI test client uses `httpx2` as a dev dependency.

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
**Consequences:** Similarity search only covers the last 90 days.

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

## D-39 — `api/` and `worker/` are sibling packages; `app/` is only the composition root
**Origin:** P0 request
**Decision:** `src/cicd_doctor/` contains `core/`, `adapters/`, `api/` (the FastAPI app, in `api/main.py`), `worker/` (the job consumer) and `app/` (settings, the wiring of adapters into use cases, and the `cicd-doctor` command in `app/cli.py`). This replaces the placement of `api.py` and `worker.py` inside `app/` that SPEC §4.2 had at first. `core/` must not import any of these packages (D-43).
**Why:** The API and the worker are inbound adapters in the hexagon (SPEC §4.1), so they sit beside `adapters/` instead of inside the composition root. `app/` keeps a single job: building the object graph. The author asked for these folders in the Phase 0 request.
**Alternatives:** The original layout (`app/api.py`, `app/worker.py`), which mixes entry points with wiring.
**Consequences:** uvicorn targets `cicd_doctor.api.main:app`. `app/settings.py` and `app/container.py` arrive in P1. D-02 still holds: `app/` is the composition root.

## D-40 — The liveness endpoint is `GET /health`
**Origin:** P0 request
**Decision:** The API answers `GET /health` with `{"status": "ok"}` while the process is serving requests. This replaces the `/healthz` name in SPEC §4.4. The readiness endpoint (the database is reachable) stays planned for P1.
**Why:** The author asked for `/health` in the Phase 0 request, and one plain name is enough for a liveness check.
**Alternatives:** `/healthz` (a common convention, but nothing in this project depends on it); serving both names (two URLs for one check).
**Consequences:** Health probes in P5 (Compose, Cloud Run) must use `/health`. The readiness endpoint, still called `/readyz` in the spec, is named when it is built in P1 so that the pair stays consistent.

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

## D-44 — `httpx2` backs the FastAPI test client
**Origin:** Proposed
**Decision:** The dev dependency group contains `httpx2` instead of `httpx` for `fastapi.testclient.TestClient`. This amends D-28 for tests only.
**Why:** The installed Starlette prefers `httpx2` and emits a deprecation warning when it falls back to `httpx`.
**Alternatives:** Keep `httpx` and tolerate the warning, which is likely to become an error in a future Starlette release.
**Consequences:** When P1 adds `httpx` as the runtime client for the GitHub adapter (D-28), check that `respx` still mocks it as expected, since the test client no longer shares that package.

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
