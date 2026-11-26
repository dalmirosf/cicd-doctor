# Documentation index

CICD Doctor is a GitHub App that explains why a GitHub Actions run failed. This folder holds the story of how
it was built, phase by phase. The design and the rules live elsewhere (see "Sources of truth"); this folder
is the *history and the method*: what each phase planned, what its spike measured, what was built in which
pull request, what was verified live, and what the method taught.

Everything here is in English, with one exception agreed with the owner: [guia-de-lectura.md](guia-de-lectura.md)
is a reading guide in Spanish (rioplatense) for reading the whole project slowly. The original working
documents under `docs/phases/*/` (`plan.md`, `spike.md`, `brief.md`) are also in Spanish, because they were
written for the owner; each one says so in its first line.

## Sources of truth

When two documents disagree, the first one in this list wins:

| Source | What it is |
|---|---|
| The code and its tests | What the application really does. |
| [`../DECISIONS.md`](../DECISIONS.md) | The decision log (`D-NN`): every decision or deviation from the spec, with the alternatives considered. |
| [`../SPEC.md`](../SPEC.md) | The design. §13 has the acceptance boxes of every phase; a box is ticked only with evidence that was observed. |
| [`../CLAUDE.md`](../CLAUDE.md) | The working rules for Claude Code sessions, the architecture in one page and "How a phase is built". |
| [`../lab/README.md`](../lab/README.md) | The lab repository and the runbooks of every live verification ("Live verification (P1c)", "(P2)", "(P3)", "Job timeouts (D-73)"), with their evidence tables. |

The plans under `docs/phases/` are *intent*: they were the working documents before the code existed. Where a
plan says something different from the sources above, the phase `README.md` lists the difference.

## Reading order

1. [`../README.md`](../README.md) and `SPEC.md` §1 to §3, to know what the product is.
2. [guia-de-lectura.md](guia-de-lectura.md) (Spanish): the whole story in plain words, with a glossary.
3. The phases in order, each `README.md` first and then, if you want the detail, its `spike.md` and `plan.md`:
   1. [`phases/p0-p1b/`](phases/p0-p1b/README.md): foundations, the queue and the webhook, the offline log pipeline,
      the rules, the LLM path (reconstructed summary, there are no plans for these phases).
   2. [`phases/p1c/`](phases/p1c/README.md): the real GitHub (check runs, sticky comment, logs, authentication).
   3. [`phases/p2/`](phases/p2/README.md): memory (embeddings, cache, similar failures, flaky and fix detection, purge).
   4. [`phases/p3/`](phases/p3/README.md): the `explain` command, suggested changes, per-repository configuration.
   5. [`phases/p4/`](phases/p4/README.md): evals at scale (public cases, the cause judge, the CI gate) and its live check on GitHub.
   6. [`phases/p5/`](phases/p5/README.md): deploy and observability (Docker, Terraform on AWS, continuous deployment, logs and traces).
4. `DECISIONS.md` from D-47 on, in the order of the phases (the phase READMEs list which `D-NN` each one produced).

## What each phase folder contains

| File | What it is |
|---|---|
| `README.md` | The story, written for this repository after the phase was verified: goal, pull requests, decisions, what the spike measured, what was verified live, what stayed open, what the method taught. |
| `plan.md` | The reviewed plan as it was approved (Spanish, untouched apart from a one-line header). |
| `spike.md` | The report of the read-only spike that preceded the code (Spanish, untouched apart from the header). |
| `brief.md` | (P2, P3 and P4) The assignment given to the planning session, with the owner's local paths replaced by `<repo>`. |
| `fixtures/` | The sanitized responses and data the spike recorded, only the ones that passed a scan (no tokens, keys, emails, JWTs, signed URLs or personal ids; the only real logins are the public `dalmirosf`, `dalmirosff` and `cicd-doctor-dev`). Each phase README says what was left out (P4 has none: its spike metadata names workflows of third-party repositories). |

The numbers in the plans (`D-NN`) were provisional when the plans were written and were assigned in merge
order, so they can differ from `DECISIONS.md`. P3 is the clearest case; its README has the mapping.

## Not in this folder

- `plans/` (untracked, local): the working drafts. A phase's plan is copied here only when the phase is verified.
- `plans/resumen p1a p1b p1c.pdf`: a PDF summary of P1a to P1c written for the owner on 2026-11-16. It is not in
  the repository; the same story is told in `phases/p0-p1b/` and `phases/p1c/`.
- Evidence tables: they live next to the runbooks in `lab/README.md` and in the ticked boxes of `SPEC.md` §13.
