"""Eval cases (SPEC §11.1): ``evals/cases/<source>/<case_id>/{case.yaml, log.txt, jobs.json}``.

Logs are redacted when they are captured and are never executed or followed: they are data.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cicd_doctor.adapters.github.jobs import parse_jobs
from cicd_doctor.core.models import Category, RawLog, RunRef, WorkflowJob

DEFAULT_CASES_DIR = Path(__file__).resolve().parents[1] / "cases"
SOURCES = ("lab", "public", "injection")
_RUN_URL = re.compile(r"/actions/runs/(?P<run>\d+)(?:/attempts/(?P<attempt>\d+))?")


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    cause: str = ""
    evidence_lines: list[int] = Field(default_factory=list)  # line numbers in log.txt


class Fix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str | None = None
    summary: str | None = None


class CaseFile(BaseModel):
    """The contents of ``case.yaml``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: Literal["lab", "public", "injection"]
    repo: str | None = None
    run_url: str | None = None
    job_id: int | None = None  # the job of log.txt; default: the first failed job
    lab_case: str | None = None  # the lab manifest entry, shared by captures of one failure
    expected: Expected
    fix: Fix | None = None
    flaky: bool = False
    tags: list[str] = Field(default_factory=list)
    captured_at: datetime | None = None


@dataclass(frozen=True)
class Case:
    meta: CaseFile
    root: Path
    jobs: list[WorkflowJob]
    log: RawLog

    @property
    def id(self) -> str:
        return self.meta.id

    def run_ref(self) -> RunRef:
        """A ``RunRef`` for the case; only the fields the offline pipeline reads matter."""
        match = _RUN_URL.search(self.meta.run_url or "")
        repo = self.meta.repo or "unknown/unknown"
        return RunRef(
            installation_id=0,
            repo_id=0,
            repo_full_name=repo,
            run_id=int(match.group("run")) if match else 0,
            run_attempt=int(match.group("attempt") or 1) if match else 1,
            workflow_id=0,
            workflow_name=self.meta.lab_case or self.meta.id,
            head_sha="0" * 40,
            head_branch="main",
            head_repo_full_name=repo,
            event="workflow_dispatch",
            is_fork=False,
            pr_number=None,
        )


def load_case(root: Path) -> Case:
    meta = CaseFile.model_validate(yaml.safe_load((root / "case.yaml").read_text("utf-8")))
    jobs = parse_jobs(json.loads((root / "jobs.json").read_text("utf-8")))
    # newline="": a bare "\r" is a progress update inside a line, not a line break (§7.3).
    text = (root / "log.txt").read_text(encoding="utf-8", newline="")
    return Case(meta=meta, root=root, jobs=jobs, log=RawLog(text=text, truncated=False))


def load_cases(
    cases_dir: Path = DEFAULT_CASES_DIR, sources: Sequence[str] = ("lab",)
) -> list[Case]:
    """Every case of the given sources, sorted by id."""
    unknown = sorted(set(sources) - set(SOURCES))
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")
    cases: list[Case] = []
    for source in sources:
        source_dir = cases_dir / source
        if not source_dir.is_dir():
            continue
        for root in sorted(path for path in source_dir.iterdir() if (path / "case.yaml").is_file()):
            case = load_case(root)
            if case.meta.source != source:
                raise ValueError(f"{root}: source {case.meta.source!r} but stored under {source!r}")
            cases.append(case)
    return sorted(cases, key=lambda case: case.id)
