# Real GitHub API fixtures

Responses of the real GitHub API, recorded **read-only** from the development app on 2026-11-16
(the spike of P1c) and **sanitized** before they entered the repository: logins, repositories,
emails, tokens, signed URLs, avatars, SHAs and ids were replaced (the owner is `acme`, ids start at
`90000000`), the app slug and the public `github-actions` app were kept. The content of these files
is not edited by hand; a test loads a file and selects what it needs.

They check that the adapters still read the real shape (`tests/adapters/test_github_fixtures.py`).
Where a response was trimmed:

- `installation_token.json`: GitHub's `token` is not recorded; the test adds a placeholder.
- `log_redirect.json`, `log_blob_headers.json`: the signed log URL is reduced to its host, a path
  template and the **names** of its query parameters; log bodies were counted and discarded.
- `log_not_found.json`, `forbidden_secrets.json`: status and error body only (the permission header
  `x-accepted-github-permissions: secrets=read` of the 403 is written in the test).
- `check_runs_lab.json`, `check_runs_me.json`: check runs of GitHub Actions; their `app` object is
  the real, large one (a long permissions list), so tests pick the fields they need.
- `app.json` and other objects of the same kind may carry only the fields the spike read.
- `hook_config.json`, `hook_deliveries.json`: no secret, no payloads.

Never add a file here without sanitizing it first, and never commit a token, a key or a signed URL.

`workflow_run_success_pull_request.json` and `workflow_run_success_push.json` are successful runs of
this repository recorded read-only on 2026-11-17 (the spike of P2) and sanitized the same way; the
tests wrap them in a webhook envelope to check that a real `success` becomes a `record_outcome` job.

`actions_runs_head_sha.json` is the answer of `GET /actions/runs?head_sha=<sha>` for the head of a lab pull request, recorded read-only on 2026-11-20 (the spike of P3, B1) and sanitized the same way; the test of `GitHubRunSource` checks that its item gives the same `RunRef` as the `workflow_run` webhook of that run. Its `pull_requests` is filled because the run belongs to a branch of the same repository: for a fork run GitHub leaves it empty (B1), which the synthetic tests of the adapter cover.
