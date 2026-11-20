# P3: commands, suggestions and per-repository configuration

Built and verified on 2026-11-20. Files in this folder: [`brief.md`](brief.md) (the assignment given to the planning
session), [`plan.md`](plan.md) (v1.1), [`spike.md`](spike.md), [`fixtures/`](fixtures/).

## Goal

SPEC FR-19 to FR-21: a maintainer comments `/cicd-doctor explain` on a pull request and gets an LLM diagnosis of
the failed runs of its head commit, also on a pull request from a fork; the model can propose a code change that is
published as a review comment with a `suggestion` block **only** when it can be applied exactly where it says;
and a repository can configure the app with `.github/cicd-doctor.yml`, read from the default branch only.

## Pull requests

| PR | Step | What it did |
|---|---|---|
| #37 | (a) | The app needs **Issues** permission for `issue_comment` and reactions (not Pull requests, as D-21 had assumed): the app gains Issues write and the `issue_comment` event, and the code gets one token permission set per use case, so an installation that has not accepted the new permission keeps diagnosing. D-89. |
| #38 | (b) | `.github/cicd-doctor.yml` (`comment`, `llm: auto|on_demand|off`, `ignore_workflows`): strict schema, 16 KB cap, a YAML event scan, default branch only, an invalid file turns the LLM off. D-90, D-91. |
| #39 | (c) | The `issue_comment` webhook: exact first line, `OWNER`/`MEMBER`/`COLLABORATOR` who is a `User`, one accepted command per PR per 5 minutes in `command_invocations`, the 👀 reaction; every refusal is silent. D-92, D-93. |
| #40 | (d1) | `explain` in the use case: it improves the already published diagnosis of an attempt in place (from the stored excerpt, no log download) with `FailureStore.upgrade_published`; a fork run reaches the model but never reads the cache and gets no similar failures. D-94, D-95. |
| #41 | (d2) | The explain job: reads the PR now, reacts 😕 when there is nothing to do, lists the runs of the head commit, picks at most 3 (the latest per workflow) and enqueues one child `diagnose_run` per run. D-96. |
| #42 | (e) | Suggestions: the PR diff goes to the model in a third untrusted block (prompt `diag-v3`), a suggestion is valid only inside ONE shown hunk, and `SuggestionPublisher` posts at most 3 review comments. D-97, D-98. |
| #43 | (f) | The injection suite for the new surfaces: 14 new cases with a diff, tables for command comments (32) and configuration files (29), an oracle for the review comment, the 34-row mutation table, three holes of (e) closed. D-99. |
| #44 | (g) | The material for the live check: the lab workflow `pr-suggest`, the config examples, the runbook "Live verification (P3)" with its consent groups. |
| #45 | | The live verification recorded: boxes 1 to 5 ticked with evidence. |

## Decisions produced, and how the plan's provisional numbers map

The plan numbered its decisions D-89 to D-97 provisionally, in a different order. The final ones, in merge order:

| Final | Plan | One line |
|---|---|---|
| D-89 | D-97 | `issue_comment` needs Issues write; the app adds the permission and the event; permission sets per use case (modifies D-21). |
| D-90 | D-95 | Schema, defaults and validation of the config file; default branch only; an invalid file turns the LLM off (modifies D-26). |
| D-91 | D-96 | `comment: false` and `ignore_workflows` against what is already published and the resolution of D-84. |
| D-92 | D-89 | Who can give the command, decided from the signed payload alone. |
| D-93 | D-90 | One accepted command per PR per 5 minutes, refusals silent. |
| D-94 | D-92 | A fork run with `explain` reaches the model with a smaller prompt, no cache and no history. |
| D-95 | D-93 | `explain` upgrades the published diagnosis in place (modifies D-69). |
| D-96 | D-91 | Which runs `explain` explains, and what it does when there are none. |
| D-97 | D-94 | Suggestions come from the diff and are checked against it. |
| D-98 | (none) | Prompt `diag-v3` with an `<untrusted_diff>` block. |
| D-99 | (none; "D-64 widened") | The injection surface of P3 and its mutation table. |

Every number from D-89 to D-97 in the plan means something different from the final decision with the same
number, so read the plan with this table next to it. In the PR descriptions and in `SPEC.md` and `CLAUDE.md` the final numbers are used.

## What the spike measured, and what changed because of it

Parts B and C of the plan (read-only `gh api` calls and local throwaway scripts); part E was the owner looking at the app
settings screen. Detail in [`spike.md`](spike.md).

- **The runs of a fork PR do appear in the base repository's list, by `head_sha`, with `pull_requests` empty** and sometimes
  `conclusion = action_required`. So D-96 matches runs by head commit **and** head repository, never by `pull_requests`, and
  `action_required` is not a failure.
- **`patch` is missing** for large generated files and for pure renames: no suggestion for those files (D-97).
- **The cost of the diff in tokens is large** (40 lines of Markdown were about 4,700 tokens), so the block is bounded in
  real tokens (2,000), not only in lines.
- **YAML:** `safe_load` of 1 MB takes 5.5 s and 64 MB; duplicate keys are accepted silently (the last wins); nesting of 500 levels raises
  `RecursionError`. Hence the 16 KB cap before parsing, an event-level scan that rejects aliases, anchors, tags, merge keys and
  duplicate keys, and a depth limit (D-90).
- **The app did not receive `issue_comment`** (Issues "No access", no "Issue comment" in the events): the plan's biggest contradiction with D-21,
  seen by the owner on the settings page before any code.
- **Not measurable read-only:** the exact 422 for a line outside the diff, the shape of a multi-line review comment, the real
  permissions of the app token by endpoint (the `x-accepted-github-permissions` header only appears with app tokens). They were measured in (g).

## Verified live (2026-11-20; every id and time is in SPEC §13 P3 and in the evidence table of "Live verification (P3)")

| Box | Evidence |
|---|---|
| 1: `explain` by a collaborator gives 👀 and an LLM diagnosis within 90 s | Lab PR #4, command comment 7051106445: 👀 after 3 s, sticky edited after 11 s, same check and sticky ids, `rules` then `llm` (USD 0.000339). **Open: the fork half** (no fork of the lab by another account was used). |
| 2: a non-collaborator gets nothing, a second command in 5 minutes is ignored | Comment 7051236689 by `dalmirosff` (association `NONE`): 0 reactions, 0 jobs, 0 rows. A second command by the owner inside the window: no reaction, nothing changed. |
| 3: a suggestion inside the diff is a review comment, outside it is text only | Inside: lab PR #5, review comment 4647976456 on line 8 of `legacy.py` with a `suggestion` block. Outside: lab PR #6, no review comment — **but both stickies came from the exact-signature cache**, so the path with a fresh model answer was not exercised live. Measured: a review comment on a line outside the diff answers `422 … could not be resolved`; a range of two lines is accepted and gives `start_line`/`line`. The adapter needed no change. |
| 4: `.github/cicd-doctor.yml` | `comment: false` on `main`: check, no comment (PR #7); a file only in a PR branch has no effect (PR #8; checked with a branch of the same repository, not a fork head); an invalid file: "configuration is invalid", 😕, `config_off`, no model call (PR #9). |
| 5: the `issue_comment` permission | The new permission accepted on the installations; the first real `issue_comment` delivery (comment 7051042761) arrived with a green tick. |

The 14 new injection cases (diff and suggestions) also ran against the real model: 1 passed, 0 attacks won.

## What stayed open, and why

- **A command on a pull request from a fork** (box 1, and the fork half of box 4): needs a fork of the lab owned by another account. `dalmirosff` exists now but was not used. Covered offline (D-94, `tests/e2e/test_explain_runs_e2e.py`, the poisoning tests of (f)). The same fork limitation keeps the fork box of P1c open.
- **The model-validated "outside the diff" path** live (the cache answered, see box 3).
- **An observed deviation:** after an explain upgrade, the unpublished `rules` row has an empty `fallback_reason` where the runbook expected `on_demand`. Recorded as observed, not analysed.
- Cells that were not captured are marked "not recorded" in the evidence table (some webhook delivery ids, the worker's `seconds_since_command` lines).

## What the method taught

- **The reviewer is not a formality.** The independent reviews recorded in the PR descriptions found: the Issues row missing in the D-21 table and a stale sentence about reactions (#37); a path check that had lost its backslash handling in a refactor and replacement text with bidirectional controls and Unicode line separators accepted ("Trojan Source in one click") after 37 hostile tests (#42); an oracle that accepted a comment body with no suggestion block (#43); the wrong column in a runbook (`ruff` reports `8:21`, not `8:12`, #44).
- **The author's own work found a real hole too:** with `explain` the policy says `llm` for a fork, so the cache guard (which only looked at the policy) would have *read the cache* for a fork; removing either guard makes a test fail (#40, D-94). Plans can anticipate it (the plan's R-P3-3 named second-order poisoning) but only a test shows it is closed.
- **Fail closed beats the spec when the spec is wrong.** D-26 said an invalid configuration gets the defaults; a typo in `llm:` would have *turned on* the model in the repository that tried to turn it off. D-90 changes it.
- **A spike can say "not measurable".** The plan's list of what the spike could not measure (the 422, the multi-line comment) became the list of probes of the live runbook and was answered there.
- **Evidence has to say which path it exercised.** Box 3's "outside the diff" was ticked with the note that the cache answered, not hidden. The same discipline kept the fork box open for the third time.

## Where the plan and the final code differ

- The numbering of decisions (above), and two decisions of the final set (D-98, D-99) that the plan did not have.
- The plan said "no migration is expected"; none was needed.
- The plan's target was 90 s from the command to the diagnosis (and said it had no p95 with one sample); the one live measurement was 11 s.
- Plan §5 item 3 and the plan's `D-92` say the explain of a fork never reads the cache; the code that existed before (d1) would have (the hole above), and (d1) closed it.

## Files in this folder

- `fixtures/`: `b1_fail.json` (runs of a head SHA), `b2_pr34.json` and `b2_pr36.json` (`GET /pulls/{n}/files` of two documentation-only pull requests). They passed the scan (owner `acme`, placeholder email `dev@acme.example`).
  **Left out:** `b1_all.json` (byte-identical to `b1_fail.json`), and `b2_pr2.json`, `b2_pr3.json`, `b2_pr4.json`: 770 KB of patches of this repository's *source code and tests*, including text shaped like secret patterns (regexes of the redaction code, in `b2_pr4.json`) and example e-mail addresses in test code (`b2_pr3.json`); they are redundant with the repository itself and do not pass the strict scan. The reviewed versions the tests use are `tests/fixtures/github/pulls_files_pr34.json` and `pulls_files_pr36.json`.
