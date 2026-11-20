# P1c: the real GitHub

Built and verified on 2026-11-16 and 2026-11-17. Files in this folder: [`plan.md`](plan.md) (v2, Spanish),
[`spike.md`](spike.md) (the read-only spike and its Appendix B, Spanish), [`fixtures/`](fixtures/).

## Goal

Make the offline pipeline of P1b work against the real GitHub: authenticate as the app, download job logs,
publish one `neutral` check run `CICD Doctor: <workflow>` and one sticky comment per pull request and workflow,
survive crashes and re-runs without duplicating anything, and prove it on the dev app, the lab repository and
this repository. P1c was the first phase to follow the method written in `CLAUDE.md` ("How a phase is built").

## Pull requests

| PR | What it did |
|---|---|
| #9 (a) | Postgres persistence of the diagnosis flow: `PostgresFailureStore` with the analysis **checkpoint** of a run attempt (first writer wins), deterministic failure ids, the **publication lock** (advisory lock), `PostgresUsageLedger` with the budget reservation, migration `0003`. D-69, D-70. |
| #10 (b1) | `GitHubHttp` (one place for headers, pagination, rate limits and the error mapping) and app authentication (RS256 JWT, installation tokens scoped to one repository and the permissions of the use case, cached). D-67, D-68. |
| #12 (b2) | `GitHubLogSource`: attempt jobs, the signed log redirect fetched without credentials, an offset `Range`, a ring buffer behind it. D-71. |
| #11 (c) | `GitHubReporter` (check run found by `external_id`, sticky comment found by its hidden marker among the bot's own comments), `GitHubPullRequests` (the PR of a run, forks included), the check name. D-72, D-76. |
| #13 (d1) | `DiagnoseFailedRun`, the use case that ties the pieces together, and the GitHub fakes (`FakeLogSource`, `FakeReporter`, `FakePullRequests`). D-75. |
| #14 (d2) | The worker diagnoses for real: `diagnose_run_handler` (publishes "Could not diagnose" when the job gives up), the container wiring, e2e tests over `FakeGitHub`, Postgres and crash tests. D-74. |
| #15 (e) | Preparation of the live check: the lab workflow `pr-py-modnf`, the runbook, the real API fixtures and the U10 fix (see below). |
| #17 | The live verification recorded: boxes 1, 2, 4, 5 and 6 ticked with evidence. |
| #18 (f) | Job-level timeouts: a `cancelled` run is diagnosed only when the annotations of its job report the timeout. D-73. |

The plan had nine pieces (a, b1, b2, c, d1, d2, e, f plus the spike); the owner asked in the review of v1 to split
(b) and (d) and to put the timeouts last and separable, which is why the plan is "v2".

## Decisions produced

- **D-67** `httpx2` with a `FakeGitHub` over `MockTransport` (no `httpx`, no `respx`) and one error mapping. **D-68** PyJWT,
  tokens per repository and per permission set, in-memory cache with one request in flight.
- **D-69** one checkpoint per run attempt, ids that are deterministic (UUIDv5), the LLM usage stored on the published
  row. **D-70** compute once, publish from what is stored, "exactly one comment".
- **D-71** the log redirect is fetched without credentials, by offset `Range`, with a ring buffer and a grace rule for
  a 404 right after the run ends. **D-72** the PR of a run, forks included (the webhook's `pull_requests` is not trusted
  alone). **D-73** job timeouts end `cancelled` and are diagnosed only when an annotation says so.
- **D-74** the handler publishes "Could not diagnose" when the job gives up. **D-75** the payload carries the account so the
  use case can ensure the installation and repository rows. **D-76** the check run is named `CICD Doctor: <workflow>`, with
  the name sanitised and cut.

## What the spike measured, and what changed because of it

The spike ran on 2026-11-16 (39 API calls, only `GET` plus three token requests, a whitelist inside the client
refused anything else). Full table in [`spike.md`](spike.md). The findings that changed the code:

- **The log URL** answers 302 to an Azure blob with a signed query; the signature expires between 2 and 10 minutes;
  a suffix `Range` is ignored (200 with the whole body), an offset `Range` gives 206. So D-71 asks for the tail by offset
  and treats a 200 to a `Range` request as a complete body.
- **Each attempt has its own job ids** (U4), so the jobs are always read with `attempts/{n}/jobs`.
- **A job-level timeout** ends `cancelled`; the text "exceeded the maximum execution time" is an annotation, readable with
  the job id as the check run id (U7). That made option (a) of D-73 viable.
- **The dev app had every permission needed** (U13) and no extra one; `workflow_run` was subscribed.
- **Appendix B (writing to a throwaway branch of the lab):** a completed check run is **not** reopened, but GitHub does not
  reject the attempt: the `PATCH` answers 200, the status stays `completed`, and `name` and `output` are applied (U10). The
  plan expected a 422. The reporter already handled both, so the code worked as it was, but its test and the note of D-70
  were wrong and PR #15 corrected them. Check run names are shown as literal text in the UI, with no escaping needed (U12, D-76).
- **Rate limit** headers and the 403 header `x-accepted-github-permissions` (it tells which permission was missing) were
  recorded; the 429 and `retry-after` branches stay covered only by fabricated responses.

## Verified live (evidence is in SPEC §13 P1c and in "Live verification (P1c)" of `lab/README.md`)

| Box | Evidence |
|---|---|
| 1: PR in the lab gives one neutral check and one sticky comment within 90 s | Lab PR #1, run 43966010866: check published 7.9 s and comment 9.4 s after the end of the run; check 133645664414, comment 6930713375. |
| 2: "Re-run failed jobs" edits, it does not duplicate | Attempt 2 of the same run: same check id, same comment id, comment now says "Attempt 2". |
| 4: invalid OpenAI key gives "LLM unavailable" and the rules diagnosis | Run 43971014722, check 133663054334, `fallback_reason = llm_error`. |
| 5: `LLM_DAILY_BUDGET_USD=0` gives the budget text and no call | Run 43972549100, check 133668325281, `llm_usage.calls` unchanged. |
| 6: a broken test in this repository is diagnosed | PR #16, checks `CICD Doctor: CI` 133672838528; two parallel runs of one PR gave **one** comment (6931758188). |
| D-73 timeouts (2026-11-17) | Run 44098408459 (timeout) gives a neutral check titled "timeout"; run 44099984808 (manual cancel) gives nothing. |
| P1a: "exactly one check run" after a worker crash | Offline, Postgres: `tests/integration/test_diagnose_crash.py`. |

## What stayed open, and why

- **Box 3, a PR from a fork** ("LLM not used for fork PRs"): the fork needs a repository owned by another account (an
  organization was planned; in P3 the account `dalmirosff` turned out to be the way). Offline, the use case and worker tests cover it.
  Still open on 2026-11-20. A check run on the head SHA of a fork (U11) was never observed either.
- Not observed live: a 404 of the logs right after completion (the grace rule of D-71), and U4's case of a job that was
  not re-run and still appears in the new attempt (the code takes it as it comes and the behaviour is documented).

## What the method taught

- **A spike pays for itself in the first hour.** U1 to U3 (the log URL, no `Content-Encoding`, the ignored suffix `Range`)
  would each have been a production bug; U10 contradicted the plan itself.
- **The sanitizer is also code.** The first sanitising pass of the spike marked 6 of 19 fixtures as blocked, and had treated
  the public `github` account as a user and rewritten `api.github.com`. It was fixed and *all* files were regenerated and
  verified by a second tool (0 real logins, repositories, emails, tokens, signatures, avatars, 0 of 25 real SHAs, 0 of 152
  real ids survive). This is the origin of the rule "fixtures are never edited by hand".
- **Split the plan where the review can't hold it.** (b) and (d) were split on the owner's review of v1.
- **Mutation as proof.** Seven mutations of the HTTP client (single flight, the 5-minute margin, the origin check, …) each made a
  test fail (#10); the ledger concurrency test fails if the advisory lock is removed (#9).
- **Parallel worktrees share a test database.** Migrating the shared `<db>_test` to `0003` broke the other branches; the
  answer was a separate `TEST_DATABASE_URL` per worktree (noted in #9 and in the project memory).
- **Live evidence found mistakes the offline suite could not:** the reporter's expectation of a 422 (U10), and the
  runbook itself: its 28 PowerShell blocks were parsed on 5.1 before being handed over (#15), a practice that continued in P2 and P3.

## Where the plan and the final code differ

- The plan marks D-67 to D-76 as provisional; they were kept as written.
- The plan expected a 422 when reopening a check run (U10); the real answer is a 200 that changes nothing but the text.
- `plan.md` mentions `p1c-spike/sanitized` with 19 files; the repository's `tests/fixtures/github/` holds the reviewed
  versions the tests use (24 files by P3, including later spikes). The copies here are the originals as the spike left them.

## Files in this folder

- `fixtures/`: the 19 sanitized responses of the spike (`annotations_job_timeout.json` … `workflow_runs_list.json`).
  They passed the scan (no tokens, keys, emails, JWTs, signed query strings or personal ids; the owner of everything is `acme`, ids start at
  `90000000`, the public `github` account was kept). `hook_config.json` carries the placeholder `https://smee.io/CHANNEL`, not the real channel.
  `log_redirect.json` has the host of the blob and the *names* of the query parameters only.
