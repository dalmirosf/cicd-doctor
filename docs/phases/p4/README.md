# P4: evals at scale

Built between 2026-11-20 and 2026-11-21, verified live on 2026-11-21. Files in this folder: [`brief.md`](brief.md)
(the assignment given to the planning session), [`plan.md`](plan.md) (v1.1), [`spike.md`](spike.md). There is no
`fixtures/` folder in this phase, see "What was left out".

## Goal

SPEC FR-22 complete: an eval set big enough to say something (25 lab, 37 public and 30 injection cases, all labelled),
every metric of §11.2 in one report, a judge for the *cause* (not only the category), and a CI gate that reads
committed answers, never calls OpenAI and fails on a macro-F1 regression of more than 5 points or on any
successful injection.

## Pull requests

| PR | Step | What it did |
|---|---|---|
| #47 | (a) | The LLM answers of the eval are committed in `evals/answers/`, keyed by the prompt fingerprint (a changed prompt is a cache miss), with spending caps and `prune`. D-100, D-108. |
| #48 | (e) | The cause judge (own model, four short texts, strict verdict, every failure is "unjudged"), the calibration tooling against the owner's labels and the judge's own injection cases. D-103, D-104. |
| #49 | (b) | Metrics: recorded model latency, cost per LLM diagnosis (NFR-03), evidence validity, bootstrap intervals, calibration (ECE) and the flaky/fix sequences judged by a hand-written oracle. D-107, D-109. |
| #50 | (c) | Tooling for public cases: `discover` and `capture-public`, licence allow-list, redaction plus an independent scanner, no diff text of third parties. D-101. |
| #51, #53 | (c2), (c3) | Pairing the failing run of a fork pull request with its fix by pull request, and finding that pull request by owner and branch. D-101. |
| #52 | (f) | The full `--variant all` report, the declared JSON schema (`schema_version: 2`), the baseline reader and the README table. D-111. |
| #54 | (g) | `evals check`, `baseline --accept` and `evals.yml` with exit codes 0 to 5. D-105, D-106. |
| #55, #56, #58 | (d) | The 37 public cases (27 from vite and prettier, then 10 more from pydantic, pytest, terraform-aws-vpc and build-push-action) and the evidence regexes. D-101. |
| #57 | (h) | `evals run --estimate` (cost of a live run before it runs) and `evals variance`. D-112. |
| #59, #60 | (h) | The live data run: answers of the three LLM variants, the judge calibrated, the baseline accepted by the owner, `pull_request` added to `evals.yml`. D-113. |
| this one | (i) | The live check of the gate on GitHub, the evidence in SPEC §13 and the archive of the phase. D-114. |

## Decisions produced

The plan numbered its decisions provisionally, so the order below is the merge order of the final ones.

| Decision | One line |
|---|---|
| D-100 | Answers are committed; the key holds the prompt fingerprint, so a bad prompt is a miss, never a silent green. |
| D-108 | A live run stops at `--max-cost-usd` and `--max-calls`. |
| D-103, D-104 | The judge sees four short texts and never a log, a diff or a category; calibration against 20 labelled pairs with agreement and weighted kappa. |
| D-107, D-109 | How latency, cost, evidence and calibration are measured; flaky and fix are judged over simulated sequences. |
| D-101 | Public cases: permissive licences only, how a pair is chosen, what is stored (a sha, file names and counts, never a diff), and that nothing is a case until the owner labels it. |
| D-111 | The report, its schema, the baseline reader and the README table. |
| D-105, D-106 | The gate: macro-F1 of `rules` and `llm+hints` over lab + public, threshold 0.05, six exit codes in the order 2, 3, 4, 1; an injection success fails whatever the F1. |
| D-112 | `--estimate` and `variance`. |
| D-113 | The judge model (`gpt-6.2-sol`), the noise (sigma 0.038, the threshold stays 0.05), NFR-03 met (USD 0.0005 per diagnosis). |
| D-114 | The live check: why a bad prompt can never end in a bare exit 1, and an open point of the similar variant on the runner. |

## What the spike measured

A read-only spike on six public repositories (about 270 API calls) and on the local runner. It found that many
failed runs of public repositories are gate or bot workflows and not builds (so the manifest needs a workflow
exclusion list), that pairing by branch does not work for pull requests from forks, that only about a third of the
candidates have a single failed job (hence the matrix rule of D-101), that the rules know nothing outside Python,
Docker and Terraform (so the public set shows the gap between rules and LLM), that the fingerprint of the prompt
changes with one character, and that an offline run of the gate is a matter of seconds. The full report is
[`spike.md`](spike.md).

## What was verified live

The offline gate on the real GitHub (SPEC §13 P4 box 4, D-114), with five throwaway pull requests (#61 to #65, never
merged, branches deleted) that ran `evals.yml` without secrets:

| PR | What it changed | Run | Exit | Job time |
|---|---|---|---|---|
| #61 | A prompt that always answers `unknown`, answers not filled | 44944417148 | 3 | 27 s |
| #62 | The same prompt, its answers filled live (USD 0.0619, 172 calls) | 44945606349 | 3 (4 on the owner's machine) | 29 s |
| #63 | A **simulated** answer that obeys an attack (edited by hand) | 44945815888 | 2 | 35 s |
| #64 | A documentation-only change under `evals/` | 44945922178 | 0 | 27 s |
| #65 | 19 stored answers **degraded by hand** to `unknown` (simulated) | 44948791575 | 1 | 27 s |

The macro-F1 of `llm+hints` fell from 0.840 to 0.096 with the bad prompt (-74.4 points) and to 0.746 with the
degraded answers (-9.4 points). `evals/baseline.json` was not touched. Earlier, on 2026-11-21 (D-113), the live
OpenAI parts ran: the answers of 92 cases in three variants, the judge calibration (90% exact agreement, kappa
0.909), the variance of three runs, and the live injection suite with 0 attacks won.

## What stayed open

- On the runner, 3 cases of the `llm+hints+similar` variant had no stored answer for the filled bad prompt, while
  the same commit gave exit 4 on the owner's machine: the retrieval of similar failures may differ between
  machines. Fixed afterwards (D-116, finding H1): the score of a similar failure had three decimals and three of
  the committed scores sat within 1e-5 of a rounding edge; it has two now.
- The CI check is not a required check of `main`: it is a repository setting and stays with the owner.
- The cause judge was calibrated with labels written by Claude and reviewed by the owner (D-113): one reading of the
  scale.
- The lab has 23 distinct failures; the memory (similar failures) shows little on this set (P2).

## What was left out

The spike's metadata (`plans/p4-spike/stage1.json`, `stage2.json`) holds names of workflows and jobs of third-party
repositories, which the repository rules keep out of git, so it is not archived. The labels of the public cases and
the calibration sheets (`plans/p4-labels/`) are not archived either. The plan, the spike report and the brief are the original
working documents, untouched except for one thing: the names of workflows of third-party repositories in one table
of the spike report were replaced by a description, because the repository rules keep them out of git (the table
says so).

## What the method taught

- A gate needs its failure shown, not only its green: three of the five check runs had to be forced with simulated
  data, and one of them (the bare exit 1) cannot happen by changing the prompt at all. Writing down which failures
  are reachable by a real change and which only by editing the data is part of the evidence.
- Putting the prompt fingerprint into the answer key was the right call: a prompt changed without a refill cannot be
  green. It also means a refill is a whole-set operation: the three LLM variants share answers, and editing one
  variant's answers moves the others' keys.
- Estimate before spending: `--estimate` said USD 0.11 for the bad-prompt refill while the plan said 0.06, because the
  gate needs all three variants; the owner approved the real number before it was spent.
- The unit tests and lint of `ci.yml` stayed green with a deliberately bad prompt; only the evals gate saw it. The
  behaviour of a prompt is not covered by ordinary tests.
- A live check on a runner finds what the local machine hides (the similar variant differed on three cases).
