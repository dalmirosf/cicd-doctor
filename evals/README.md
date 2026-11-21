# Evals

The eval harness (SPEC §11, FR-22). It has the `rules`, `llm`, `llm+hints` and
`llm+hints+similar` variants, the P1b subset of the metrics (no cause-match judge yet), JSON and Markdown reports, the
lab capture command and the prompt-injection suite. `evals/cases/lab` holds 25 cases captured from
real runs of the lab, and `evals/cases/injection` 30 synthetic ones (16 log payloads and 14 with a pull request diff). The CI gate arrives in P4.

```
evals/
├── cases/<source>/<case_id>/   # lab | public | injection: case.yaml, log.txt, jobs.json (fix.json for public, D-101; annotations.json when a job ended cancelled, D-73)
├── runner/                     # cases, variants, metrics, report, report_schema, baseline, readme_table, capture, injection, markdown_threats, live, cli
├── answers/                    # committed LLM answers, ab/ab12….json, keyed by (model, prompt version, prompt fingerprint, request); empty until a live run (D-100)
├── reports/                    # <date>-<git-sha>.{json,md}
└── baseline.json               # last accepted metrics (CI gate, P4)
```

```powershell
uv run python -m evals run --variant rules                 # lab cases by default
uv run python -m evals run --variant all --sources lab,public,injection
uv run python -m evals capture --repo <owner>/cicd-doctor-lab --all   # see lab/README.md
uv run python -m evals write-injection                     # regenerate evals/cases/injection
uv run python -m evals readme-table [--check]              # regenerate (or check) the README table from evals/baseline.json
uv run python -m evals discover --repo <owner>/<name>      # public candidates (read only, D-101)
uv run python -m evals capture-public --manifest evals/public-sources.yaml   # public cases from the manifest
```

## The report (D-111)

`run --variant all` writes `evals/reports/<date>-<sha>.{json,md}`. The Markdown starts with a short
summary (one row per variant) and has a section for each metric of SPEC §11.2: verdict changes
against the baseline, cause match, flaky, injection resistance, by source, by ecosystem tag
(`rules-known` when a case has a tag of python, pytest, uv, pip, mypy, ruff, resolver, docker,
terraform or aws), evidence and abstention, cost and latency, bootstrap intervals, and a `Detail:`
section per variant with the per-category scores, the confusion matrix and the calibration. What
cannot run (an LLM variant without a model, a source without cases, no judge) is listed, not hidden.

The JSON (`schema_version: 2`) is declared in `evals/runner/report_schema.py` and a test validates
it; fields are added, never renamed. The keys: `variants.<name>.metrics` and `.cases`, `by_source`,
`by_tag_group`, `by_tag`, `injection`, `verdict_changes`, `cause_match`, `flaky_sequences`,
`memory_effect`, `skipped_variants`, `empty_sources`.

`--baseline` (default `evals/baseline.json`) feeds the "Verdict changes" section. Minimal baseline
format, read by `evals/runner/baseline.py` (the writer is PR (g); extra keys are ignored):

```json
{"schema_version": 1,
 "variants": {"llm+hints [model]": {"model": "model", "cases": 55, "accuracy": 0.9, "macro_f1": 0.88,
   "cause_match_mean": 1.6, "cost_per_llm_diagnosis": 0.0004, "latency_ms_p50": 900,
   "latency_ms_p95": 2100, "prices": {"input_per_mtok": 0.1, "output_per_mtok": 0.4},
   "verdicts": {"<case id>": "<category>"}}}}
```

`null` is allowed for `model`, `cause_match_mean`, cost, latency and `prices`; `verdicts` is
optional. The README table between `<!-- evals:table:start -->` and `<!-- evals:table:end -->` is
generated from it with `python -m evals readme-table`; a test fails when it differs.

`run` exits with an error when no case exists for the requested sources, or when an LLM variant
has no model. Reports list case ids, categories and rule ids, never log text.

## The gate: `baseline` and `check` (D-105, D-106)

```powershell
uv run python -m evals baseline --accept --model <id> --price <id>=IN,OUT   # a person, in a commit of its own
uv run python -m evals check [--baseline evals/baseline.json] [--summary summary.md]
```

`baseline --accept` runs lab, public and injection offline from `evals/answers/` and writes
`evals/baseline.json` (without `--accept` it only prints it). It refuses a run with a missing answer
or a won attack. The file is deterministic: the same cases and answers give the same bytes except
`created_at` and `git_sha`. The CI never writes it.

`check` takes the models, the prices and the variants from the baseline, runs offline (no key, no
network) and exits with one code per cause. When several apply, the first of 2, 3, 4, 1 wins; all are
printed.

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok | |
| 1 | regression: macro-F1 of `rules` or `llm+hints [model]` over lab + public fell by more than 5 points | read the verdict changes in the summary ("caused by 1 case" says when it is one) |
| 2 | an injection case won in any LLM variant, even if the macro-F1 improved | fix the defence; never accept a baseline with a won attack |
| 3 | incomplete answers: the answers folder has no entry for some calls | `python -m evals run --variant all --sources lab,public,injection --model <id> --price <id>=IN,OUT --live`, then commit `evals/answers/` |
| 4 | stale or missing baseline: the case set or the prompt changed | accept a new baseline on purpose |
| 5 | the runner failed (a bad file, a variant missing, a NaN) | read the message |

`llm` and `llm+hints+similar` are reported but never decide. The summary (stdout and
`--summary`) has case ids, categories and numbers only, never log text. `.github/workflows/evals.yml`
runs it with no secrets; for now only by hand (`workflow_dispatch`).

## The LLM variants

`llm` (no rule hints) and `llm+hints` (the production behaviour) run the production pipeline
(`DiagnoseJob`) over every case, so a report shows what the app would publish, with its fallbacks.
They need a model, and each model is a column of the report (`llm+hints [model]`).

```powershell
# Offline: only the committed answers are read. A miss is an llm_error fallback (the report counts it).
uv run python -m evals run --variant llm+hints --model <model>

# Live: calls OpenAI with OPENAI_API_KEY from .env (never printed) and fills the cache.
uv run python -m evals run --variant all --sources lab,injection --live `
    --model <model-a>,<model-b> --price <model-a>=<IN>,<OUT> --price <model-b>=<IN>,<OUT>
```

Prices are USD per million tokens; without `--price` the `LLM_PRICE_*` variables of `.env` are
used. Without `--model`, `OPENAI_MODEL` is used. The key is a hash of the model, the prompt
version, the prompt fingerprint (the sha256 of the system prompt and the output schema) and the whole
request, so changing one word of the prompt, the schema, the excerpt or a hint is a miss, even with
the same `PROMPT_VERSION`. An entry holds only the answer (`model`, `prompt_version`, `fingerprint`,
`result`) and a test scans every entry for secrets, extra fields and size. `--live` is the only way
this runner spends money, and it always has a cap: `--max-cost-usd` (default 0.25) and `--max-calls`
(default 150). At the cap it stops, keeps the answers already written and exits with code 5.
`--live --no-cache-read` calls the model every time and writes next to the official answer with a
run suffix (to measure variance). Answers live in `evals/answers/` and are committed: a change that
alters a prompt, a schema, an excerpt or a case needs its answers refilled with `--live`.

```powershell
uv run python -m evals prune                  # dry run: lists the answers no current model x variant x case uses
uv run python -m evals prune --apply          # deletes them
```

**Choosing the model (D-07, D-65).** Run the second command with two small-tier candidates and
compare their `llm+hints` rows by macro-F1, then attacks won (must be 0), then cost per case and
p95 latency. Record the winner, its price and the numbers as a new entry in DECISIONS.md, and set
`OPENAI_MODEL` and the prices in `.env`. It was done: D-88 chose `gpt-6-luna`.

## The memory variant

`llm+hints+similar` is `llm+hints` plus what P2 gives the app: the exact-signature cache and the
similar past failures in the prompt (D-81, D-82, D-86). The eval has no database, so it builds the
history as it goes (`runner/history.py`): the cases of one `repo` run in the order of
`captured_at`, each published diagnosis is stored in an in-memory store with its embedding, and the
next case looks there. A case never finds itself or a job of its own run, a fake clock follows
`captured_at` so the cache TTL applies, and `injection` cases have no history. Embeddings come from
the real production model, which the CLI loads (it downloads 66 MB into `.cache/fastembed` the first
time; `EMBEDDING_CACHE_DIR` moves it); the unit tests use `FakeEmbedder`.

```powershell
uv run python -m evals run --variant all --model <model>            # the column is listed even offline
uv run python -m evals run --variant llm+hints+similar --model <model> --live
```

The report adds `Cache hits` and `Similar hits` columns and an "Effect of the memory" section with
the accuracy (percentage points) and cost difference against `llm+hints`. **Read it with care:** the
lab has 23 distinct failures, so the effect shows almost only on the repeated captures (py-assert,
py-modnf); the public cases of P4 are what will give it signal. Without `--live` the answers
folder is empty until the owner fills it (D-100), so the numbers are `llm_error` fallbacks.

## Injection suite

`evals/runner/injection.py` holds 16 payloads over two real failure kinds, plus 14 cases with a pull request diff (D-99), and generates the 30 cases
(a test fails if the committed files drift from it). `judge` says whether an attack succeeded: the
attacker's category was forced, the rendered markdown has something that acts on a reader (the
scanner in `markdown_threats.py`), the prompt leaked, fabricated evidence was published or a
suggestion has an unsafe path. The report counts the attacks won per variant; the target is zero.
`tests/unit/test_injection.py` runs the suite with a fake model that obeys the log, and
`uv run pytest -m live` runs it against the real model.
