# Evals

The eval harness (SPEC §11, FR-22). As of P1b it has the `rules`, `llm` and `llm+hints`
variants, the P1b subset of the metrics (no cause-match judge yet), JSON and Markdown reports, the
lab capture command and the prompt-injection suite. `evals/cases/lab` holds 24 cases captured from
real runs of the lab, and `evals/cases/injection` 16 synthetic ones. Similarity (`llm+hints+similar`)
arrives in P2, and the CI gate in P4.

```
evals/
├── cases/<source>/<case_id>/   # lab | public | injection: case.yaml, log.txt, jobs.json (fix.diff for public)
├── runner/                     # cases, variants, metrics, report, capture, injection, markdown_threats, live, cli
├── .cache/                     # LLM answers keyed by (model, prompt version, request); empty until a live run
├── reports/                    # <date>-<git-sha>.{json,md}
└── baseline.json               # last accepted metrics (CI gate, P4)
```

```powershell
uv run python -m evals run --variant rules                 # lab cases by default
uv run python -m evals run --variant all --sources lab,public,injection
uv run python -m evals capture --repo <owner>/cicd-doctor-lab --all   # see lab/README.md
uv run python -m evals write-injection                     # regenerate evals/cases/injection
```

`run` exits with an error when no case exists for the requested sources, or when the variant is
not available yet. Reports list case ids, categories and rule ids, never log text.

## The LLM variants

`llm` (no rule hints) and `llm+hints` (the production behaviour) run the production pipeline
(`DiagnoseJob`) over every case, so a report shows what the app would publish, with its fallbacks.
They need a model, and each model is a column of the report (`llm+hints [model]`).

```powershell
# Offline: only the cache is read. A miss is an llm_error fallback (the report counts it).
uv run python -m evals run --variant llm+hints --model <model>

# Live: calls OpenAI with OPENAI_API_KEY from .env (never printed) and fills the cache.
uv run python -m evals run --variant all --sources lab,injection --live `
    --model <model-a>,<model-b> --price <model-a>=<IN>,<OUT> --price <model-b>=<IN>,<OUT>
```

Prices are USD per million tokens; without `--price` the `LLM_PRICE_*` variables of `.env` are
used. Without `--model`, `OPENAI_MODEL` is used. The cache key is a hash of the model, the prompt
version and the whole request, so changing the prompt, the excerpt or a hint is a miss. An entry
holds only the answer. `--live` is the only way this runner spends money.

**Choosing the model (D-07, D-65).** Run the second command with two small-tier candidates and
compare their `llm+hints` rows by macro-F1, then attacks won (must be 0), then cost per case and
p95 latency. Record the winner, its price and the numbers as a new entry in DECISIONS.md, and set
`OPENAI_MODEL` and the prices in `.env`. This has not been done yet: it waits for the API key.

## Injection suite

`evals/runner/injection.py` holds 16 payloads over two real failure kinds and generates the cases
(a test fails if the committed files drift from it). `judge` says whether an attack succeeded: the
attacker's category was forced, the rendered markdown has something that acts on a reader (the
scanner in `markdown_threats.py`), the prompt leaked, fabricated evidence was published or a
suggestion has an unsafe path. The report counts the attacks won per variant; the target is zero.
`tests/unit/test_injection.py` runs the suite with a fake model that obeys the log, and
`uv run pytest -m live` runs it against the real model.
