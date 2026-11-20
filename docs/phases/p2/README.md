# P2: memory

Built between 2026-11-17 and 2026-11-18. Files in this folder: [`brief.md`](brief.md) (the assignment),
[`plan.md`](plan.md) (v3, "decisions closed"), [`spike.md`](spike.md), [`fixtures/`](fixtures/) (all Spanish except fixtures).

## Goal

Give CICD Doctor a memory (SPEC FR-08b, FR-15 to FR-18): embed every failure, answer an identical failure from a
cache of verified model answers (no new model call), list similar earlier failures with the commit that fixed them,
recognise **flaky** failures (the same commit passes on a re-run) and **fixes** (the first green commit on the same
branch), rewrite the comment accordingly, purge old data every day, and measure the effect with a new eval variant.

## Pull requests

PR numbers are the order of merge; the letters are the plan's steps.

| PR | Step | What it did |
|---|---|---|
| #21 | (a) | `FastEmbedEmbedder` (bge-small, 384 dimensions): loaded in a background thread, `embed` fails fast with `EmbedderUnavailable` until it is loaded; the use case embeds before the model call and the vector travels in the checkpoint. `actions/cache` for the model in CI. D-77, D-78. |
| #22 | (b) | `similar` in Postgres (an exact search per repository, no forks) and a hardened `cached_diagnosis`; pgvector image pinned to `0.8.7-pg17`. D-79, D-80, D-81. |
| #23 | (c) | The exact-signature cache in the flow: `reuse_cached` re-anchors the stored evidence by text in the new excerpt, a hit reserves no budget, the report says "cached from run #N". D-81. |
| #24 | (f) | The daily purge and the scheduler (one `purge:<date>` job at 03:00 UTC). Migration `0005`. D-85. |
| #25 | (d) | Similar failures in the prompt (`untrusted_similar`, prompt `diag-v2`) and in the report. D-82. |
| #26 | (e1) | `core/outcomes.py` (a pure state machine), `FailureStore.record_success`/`later_successes`/`resolve`, migration `0006`. D-83, D-84. |
| #27 | (g) | The eval variant `llm+hints+similar` over an in-memory history ordered by `captured_at`. D-86. |
| #28 | (e2a) | A success webhook becomes a `record_outcome` job that resolves failures in the database; a late failure resolves itself against later successes. |
| #29 | (e2b) | The comment banner ("✅ Fixed by …", "Passed on re-run … recorded as flaky"), rewritten under the publication lock; the check run is never touched. D-84. |
| #30, #31 | (h) | The runbook and the `pr-flaky` lab workflow, then the live evidence (boxes 5, 6, 3, 1 and the purge). |
| #32 | | The worker silences every HTTP client logger (D-87): the model download printed request URLs at INFO. |
| #33 | | The live smoke and the mini-eval; `gpt-6-luna` chosen (D-88). |
| #34, #35 | | Box 2 (the cache) with live evidence; the real numbers of box 8. |
| #36 | | The live injection suite of P1b ticked (0 attacks won). |

The plan proposed splitting (e2) in two if it grew; it did, hence #28 and #29.

## Decisions produced

- **D-77** the embedding is computed in the use case before the model and travels in the checkpoint; an embedder error degrades, it never fails the job.
- **D-78** the model lives in the worker process, warms up in the background, caches in a fixed directory and has a switch.
- **D-79** `similar` is an exact search per repository, no forks; the HNSW path is behind a switch; pgvector pinned.
- **D-80** `SIMILAR_MIN_SCORE` stays at 0.85.
- **D-81** the cache key is `(repository, signature_hash, prompt_version)`; only verified model answers of the current prompt, never from a fork, with their origin.
- **D-82** similar failures reach the prompt as untrusted data and the report as ids (never URLs from stored text).
- **D-83** the success path: which failures a success resolves, and why no arrival order loses a resolution.
- **D-84** a resolution remembers which success made it; only the comment says "resolved".
- **D-85** the purge is a daily job with a date dedupe key, idempotent, in batches.
- **D-86** the variant `llm+hints+similar` replays history in memory and stays deterministic across platforms.
- **D-87** silence the HTTP client loggers. **D-88** `gpt-6-luna` is the model.

## What the spike measured, and what changed because of it

Parts A and B (fastembed on Windows and in `python:3.13-slim`), C (pgvector in a throwaway container) and D (`GET`s
with `gh`), on 2026-11-17. Detail in [`spike.md`](spike.md). It changed four decisions of the first plan (v1 became v2):

1. **D-78 changed.** A cold download of the model took 12 s once and 162 to 166 s three times (the load with the
   model on disk: 0.1 s). A worker cannot wait that long, so the warm-up runs in the background and the failure
   is stored without an embedding if the model is not there yet.
2. **D-79's recommendation changed.** At 20,000 rows in one repository the HNSW index without iterative scan has recall
   0.84 and returns fewer than `k` rows, while an exact scan takes 5 to 13 ms with recall 1. So: exact by default, HNSW as an option.
   The trick `ORDER BY (embedding <=> q) + 0` (so the planner cannot use the index) was written down as a *hypothesis*
   that the test of (b) had to prove with `EXPLAIN` before anything depended on it; it held (#22).
3. **D-80 was confirmed.** The hypothesis "bge-small leaves a high floor between unrelated texts" was wrong (median of negatives 0.63 to 0.69),
   but 0.85 gave recall 11/11 and one false positive in 253 pairs. The honest limit: the 11 positives were made by editing real logs, the "same error
   with another tool message" case was not in the sample.
4. **D-83.** `run_started_at` changes with every attempt, so it cannot order runs; `(run_id, attempt)` can (0 inversions in 108 runs).
   69 of 71 runs of this repository are successes, so the success path must be cheap.

Also measured: +200 MB image, 430 to 480 MB resident (a 512 MB limit survived with no margin, a note for P5), latency
4 to 14 ms per attempt, and vectors identical bit by bit between Windows and Linux. Detail: that last point was later
nuanced by the real CI: a GitHub runner CPU differed by about 1e-4, so the test of the fixture vectors uses cosine ≥ 0.9999 and
a maximum difference below 1e-3 (SPEC §13 P2 box 1).

## Verified live (2026-11-18; evidence in SPEC §13 P2 and "Live verification (P2)" of `lab/README.md`)

| Box | Evidence |
|---|---|
| 1: embeddings of 384 dimensions, < 200 ms | 9 failures stored that day have a 384-d embedding; batch of 1 p50 5.3 ms, batch of 3 p50 19.3 ms (test on the dev machine). |
| 2: an identical failure within 7 days is `cache` and does not call the model | Run 44418654562 (A) `llm`, run 44419250142 (B) `cache`, `cached_from` = the row of A, `llm_usage.calls` stayed at 1; check run says "cached from run #44418654562". |
| 3: a similar failure lists the earlier one with its fix commit | Lab PR 3, run 44387458250, three similar runs at score 0.91, one "fixed by `ef39a8e473b4`". |
| 5: flaky | Lab PR 2, run 44381406019: comment edited 2 s after the re-run passed ("recorded as flaky"); the check run kept its failure title. |
| 6: fix | Lab PR 3: commit ef39a8e fixed it (comment says "Fixed by `ef39a8e`"); a revert (a23c628) brought the diagnosis back in the same comment. |
| 7: purge | The scheduler created `purge:2026-11-18`; after a change of hour the worker logged the run with 0 rows (the dev database has nothing old). |
| 8: the eval variant | `llm+hints+similar` accuracy 92.0%, the memory saves two model calls (23 instead of 25) and changes no answer. A mechanism check, not a measurement (the lab has 23 distinct failures). |

Box 4 (isolation between repositories) is a contract test on memory and Postgres.

## What stayed open, and why

- Nothing of P2 itself. The fork box of P1c stayed open (no second account).
- The 9 failures stored before P2 have no embedding (no backfill, by decision).
- The memory cannot be *measured* with 25 lab cases; the public cases of P4 are what will do it.

## What the method taught

- **Tests find races the author did not plan for.** The two-engine purge test found a deadlock between `purge` and `purge_repositories` (#24);
  writing the comment-rewrite tests found that a success recorded between the diagnosis' read and its publish would publish
  "Likely cause" on an already resolved failure, and the fix (re-read under the comment lock) came with a test that forces the interleaving (#29).
- **A decision can be wrong in the plan and right in the spike.** D-78 and D-79 changed before a line was written, at the cost of a 2-hour read-only session.
- **The plan's own numbering slips.** The plan said the purge would take migration `0006`; the purge was merged first and took `0005`, so the outcomes took `0006` (#24, #26). Provisional `D-NN` are assigned in merge order for this reason.
- **Prices and models are not guesses.** The choice of `gpt-6-luna` came from a measured mini-eval (95.1% vs 82.9% accuracy, about USD 0.0003 per case) and the note in #33 says the prices had still to be confirmed by the owner.
- **A rule per library breaks the day a dependency changes.** The live run showed `httpx` request lines at INFO while the model downloaded. Nothing leaked (the URLs were public), but D-87 closed the whole class, because a signed log URL would have looked the same.

## Where the plan and the final code differ

- Plan: D-77 to D-86 provisional. Final: the same, plus D-87 and D-88 added while verifying live.
- Plan: purge migration `0006`, outcomes later. Final: purge `0005`, outcomes `0006`.
- Spike: vectors "identical bit by bit between Windows and Linux". Real CI: a 1e-4 difference on a GitHub runner CPU (see above).
- Plan: `SIMILAR_MIN_SCORE` could be edited only after telling the owner; it was not edited.

## Files in this folder

- `fixtures/`: `pairs.json` (264 labelled pairs with their cosine on Windows and on Linux) and five sanitized `workflow_run` bodies
  (`success` on pull request, push and dispatch; `failure` attempt 1 and 2). They passed the scan (owner `acme`, emails `dev@example.invalid`, no tokens or real ids).
  **Left out:** `lab_signatures.json` (257 KB), because it is already in the repository, identical as data, at `tests/fixtures/embeddings/lab_signatures.json`.
