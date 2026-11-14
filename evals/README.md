# Evals

The eval harness (SPEC §11, FR-22). As of P1b it has the `rules` variant, the P1b subset of the
metrics (no LLM judge yet), JSON and Markdown reports, and the lab capture command. `evals/cases/lab` holds 24 cases captured from real runs of the lab. The LLM
variants arrive with the LLM client (P1b), similarity in P2, and the CI gate in P4.

```
evals/
├── cases/<source>/<case_id>/   # lab | public | injection: case.yaml, log.txt, jobs.json (fix.diff for public)
├── runner/                     # cases, variants, metrics, report, capture, cli
├── reports/                    # <date>-<git-sha>.{json,md}
└── baseline.json               # last accepted metrics (CI gate, P4)
```

```powershell
uv run python -m evals run --variant rules                 # lab cases by default
uv run python -m evals run --variant all --sources lab,public,injection
uv run python -m evals capture --repo <owner>/cicd-doctor-lab --all   # see lab/README.md
```

`run` exits with an error when no case exists for the requested sources, or when the variant is
not available yet. Reports list case ids, categories and rule ids, never log text.
