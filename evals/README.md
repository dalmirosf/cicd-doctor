# Evals

Placeholder for the eval harness (SPEC §11, FR-22). The runner arrives in P1 (skeleton) and P4 (full).

Planned layout:

```
evals/
├── cases/<source>/<case_id>/   # lab | public | injection: case.yaml, log.txt, jobs.json, fix.diff
├── runner/                     # python -m evals run --variant all --sources lab,public,injection
├── reports/                    # <date>-<git-sha>.{json,md}
└── baseline.json               # last accepted metrics (CI gate)
```
