# Evals

The eval harness (SPEC §11, FR-22). It has the `rules`, `llm`, `llm+hints` and
`llm+hints+similar` variants, the P1b subset of the metrics (no cause-match judge yet), JSON and Markdown reports, the
lab capture command and the prompt-injection suite. `evals/cases/lab` holds 25 cases captured from
real runs of the lab, and `evals/cases/injection` 30 synthetic ones (16 log payloads and 14 with a pull request diff). The CI gate arrives in P4.

```
evals/
├── cases/<source>/<case_id>/   # lab | public | injection: case.yaml, log.txt, jobs.json (fix.diff for public; annotations.json when a job ended cancelled, D-73)
├── runner/                     # cases, variants, metrics, report, capture, injection, markdown_threats, live, cli
├── answers/                    # committed LLM answers, ab/ab12….json, keyed by (model, prompt version, prompt fingerprint, request); empty until a live run (D-100)
├── reports/                    # <date>-<git-sha>.{json,md}
└── baseline.json               # last accepted metrics (CI gate, P4)
```

```powershell
uv run python -m evals run --variant rules                 # lab cases by default
uv run python -m evals run --variant all --sources lab,public,injection
uv run python -m evals capture --repo <owner>/cicd-doctor-lab --all   # see lab/README.md
uv run python -m evals write-injection                     # regenerate evals/cases/injection
```

`run` exits with an error when no case exists for the requested sources, or when an LLM variant
has no model. Reports list case ids, categories and rule ids, never log text.

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
