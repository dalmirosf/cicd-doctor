"""Build eval case directories from the synthetic log fixtures, for the runner's own tests.

These cases are written to a temporary directory only: the committed eval cases under
``evals/cases`` are captured from real runs (``python -m evals capture``).
"""

import json
import shutil
from pathlib import Path

import yaml

from tests.logcases import LOG_CASES_DIR

# fixture -> (expected category, job id of the log)
SYNTHETIC = {
    "pytest-assert": ("broken_test", 71000000001),
    "py-modnf": ("dependency", 71000000002),
    "job-timeout": ("timeout", 71000000003),
    "docker-copy": ("config_or_secrets", 71000000004),
    "no-step-data": ("dependency", 71000000021),  # npm 404: the rules cannot tell
}


def write_case(
    cases_dir: Path,
    fixture: str,
    *,
    case_id: str | None = None,
    source: str = "lab",
    category: str | None = None,
    lab_case: str | None = None,
) -> Path:
    expected_category, job_id = SYNTHETIC[fixture]
    case_id = case_id or f"{source}-{fixture}-01"
    root = cases_dir / source / case_id
    root.mkdir(parents=True)
    shutil.copyfile(LOG_CASES_DIR / fixture / "jobs.json", root / "jobs.json")
    shutil.copyfile(LOG_CASES_DIR / fixture / "logs" / f"{job_id}.txt", root / "log.txt")
    expected = json.loads((LOG_CASES_DIR / fixture / "expected.json").read_text("utf-8"))
    case = {
        "id": case_id,
        "source": source,
        "repo": "acme/cicd-doctor-lab",
        "run_url": "https://github.com/acme/cicd-doctor-lab/actions/runs/123/attempts/1",
        "job_id": job_id,
        "lab_case": lab_case or fixture,
        "expected": {
            "category": category or expected_category,
            "cause": "synthetic",
            "evidence_lines": expected["evidence_lines"][str(job_id)],
        },
        "tags": ["synthetic"],
    }
    (root / "case.yaml").write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")
    return root
