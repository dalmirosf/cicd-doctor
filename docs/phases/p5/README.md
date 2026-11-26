# P5: deploy and observability

Built between 2026-11-24 and 2026-11-25, verified live on 2026-11-25. Files in this folder: [`plan.md`](plan.md) (v1).
There is no `spike.md` and no `fixtures/` folder in this phase, see "What was left out".

## Goal

Run the application somewhere other than the developer's machine, for a short and cheap session: one container image,
Terraform for AWS, a deploy from `main` with no long-lived key, JSON logs and traces of the model calls (SPEC §13 P5).

## Pull requests

| PR | Step | What it did |
|---|---|---|
| #70 | (a) | Structured JSON logs with structlog, correlated by `delivery_id` and redacted. D-117. |
| #71 | (c) | One multi-stage image for the api, the worker and the migration, with the models baked in; the full local stack in Compose. D-119. |
| #72 | (d) | Base Terraform: state bucket, ECR, secret containers, IAM roles, network, RDS, cluster (code only). D-121, D-122, D-124, D-125. |
| #73 | (b) | LLM traces in Langfuse behind a `Tracer` port, redacted, never in the way of a diagnosis. D-118. |
| #74 | (e) | The database URL built from five parts. D-126. |
| #75 | (e) | Services, load balancer, CloudFront, migration task, OIDC provider and deploy role. D-120, D-123, D-127. |
| #76 | | `core/` may not import `langfuse` or `opentelemetry`; the tracer warns once per process. D-118. |
| #77 | (f) | `cd.yml`: build, push, new task definition revisions, migration, then the services. D-128. |
| #78 | (h) | The live verification runbook; the Terraform GitHub provider for the lab dropped. D-129. |
| #79 | (h) | A one-line documentation change, merged with the stack up to exercise the deploy. |
| #80 | (h) | The two defects the live run found, and the "Quick up and down" runbook. D-130. |
| this one | (i) | The evidence in SPEC §13 and the archive of the phase. |

## Decisions produced

| Decision | One line |
|---|---|
| D-117 | structlog over the stdlib, one JSON object per line, correlation by context variables, redaction as the last processor. |
| D-118 | A trace per real model call; only redacted, capped text leaves the process. |
| D-119 | One image, the command decides; non-root; the embedding model and the tokenizer vocabulary baked in. |
| D-120 | The HTTPS entry without a domain: CloudFront with its default certificate in front of an HTTP load balancer. |
| D-121 | `us-east-2`; tasks in public subnets without a NAT gateway, the database private. The tradeoff is written there. |
| D-122 | Secret containers in Terraform, values loaded by hand, task roles with no permission. |
| D-123, D-128, D-130 | Deploy through OIDC pinned to `main`; the migration runs as a task before the services; a destroyed stack is not an error. |
| D-124, D-125 | Three Terraform layers; "down" is a destroy of the one that bills by the hour. |
| D-126 | `Settings` builds the database URL from `DB_*`. |
| D-127 | A `://` outside a URL is escaped in untrusted prose (found by the fuzz test). |
| D-129 | The lab repository stays managed by hand. |

## Differences from the plan

- The region is `us-east-2`, not `us-east-1`: an organization policy denies workloads elsewhere (D-121).
- Step (g), the Terraform GitHub provider, was dropped (D-129).
- The decision numbers follow the merge order.

## What was verified live

On 2026-11-25, with the production app installed only on the lab and `gpt-6-luna`. The ids, times and outputs of every
check are in SPEC §13 P5 and in the evidence table of "Live verification (P5)" in
[`infra/terraform/README.md`](../../../infra/terraform/README.md).

| Box of SPEC §13 | Observed |
|---|---|
| 1, image and Compose | Ready in 39 s, non-root, 790 MB. |
| 2, Terraform | 7 + 14 + 44 resources; a second `plan` with no changes; migrations `0001` to `0006` on RDS. |
| 3, GitHub provider | Dropped (D-129). |
| 4, deploy from `main` | A merge deployed new revisions after the migration, with no stored key; with the stack down it ends green. |
| 5, production app | A pull request diagnosed in AWS: the check 11 s after the run ended, the same check and comment on a re-run. |
| 6, logs and traces | One delivery followed from the api to the worker; a planted fake key redacted in the comment and in the trace output, absent from the logs. |

The ephemeral layer was up for about 1 h 31 min and was destroyed; the checklist that follows a destroy found nothing
billing by the hour.

## What the live run found

- The GitHub account issues **immutable** OIDC subjects (with numeric ids), so the first deploy was refused (D-130).
- `cd.yml` died under `bash -e` before it could report a missing cluster (D-130).
- GitHub masks a token of its own format before the app sees the log, so the first planted secret proved nothing.
- The Langfuse input is cut at 4,000 characters, so the second planted secret, far down the excerpt, was not in it
  either. The third one, inside the error line, showed the redaction in the model's answer.
- A first answer of the model came back as broken JSON; the repair call fixed it, and both are traces.
- Two workflows fail on a lab pull request, so it gets two comments; the runbook expected one.

## What stayed open

- The real cost was not read from Cost Explorer (estimate: about USD 0.11 an hour up).
- `main` has no branch protection. The deploy role trusts every run on `main`, so D-123 (5b) asks for it.
- The path for a cluster that does not exist at all was exercised only in a local simulation.
- Against production only boxes 1 and 2 of P1c were repeated.
- A text search over a whole Langfuse trace for the planted key was not recorded.

## What was left out

No spike report was written for this phase: what the local experiments measured is inside D-117 and D-119, and the
read-only checks of AWS inside D-121. Account ids, resource addresses and the log files copied before the destroy are
not archived.

## What the method taught

- Code that is only validated offline hides the facts of the other side: both deploy defects needed a real run.
- A security check needs proof that the secret reached the thing under test. Twice the search came back clean for a
  reason that had nothing to do with the redactor.
- Splitting what bills by the hour from what does not made the teardown one command and the next start short.
