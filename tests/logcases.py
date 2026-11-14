"""Load the log fixtures (``tests/fixtures/logs/<case>/``) and run the excerpt pipeline on them.

Each case has ``jobs.json`` (one page of the GitHub jobs API), ``logs/<job_id>.txt`` (job logs in
GitHub's format) and ``expected.json`` (the jobs that must be selected and the evidence lines of
each). The cases are synthetic, written to look like real logs; the lab cases captured from real
runs live in ``evals/cases/lab`` (SPEC §11.1).
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cicd_doctor.adapters.github.jobs import parse_jobs
from cicd_doctor.core.logs import ExcerptConfig, build_excerpts, render_line, select_failed_jobs
from cicd_doctor.core.models import FailedJob, LogExcerpt, RawLog, WorkflowJob
from cicd_doctor.core.ports import TokenCounter
from tests.factories import make_run_ref

LOG_CASES_DIR = Path(__file__).parent / "fixtures" / "logs"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"


@dataclass(frozen=True)
class LogCase:
    name: str
    root: Path
    jobs: list[WorkflowJob]
    expected: dict[str, Any]

    @property
    def selected_job_ids(self) -> list[int]:
        return [int(job_id) for job_id in self.expected["selected_job_ids"]]

    def evidence_lines(self, job_id: int) -> list[int]:
        return [int(n) for n in self.expected["evidence_lines"].get(str(job_id), [])]

    def raw_log(self, job_id: int) -> RawLog:
        text = (self.root / "logs" / f"{job_id}.txt").read_text(encoding="utf-8", newline="")
        return RawLog(text=text, truncated=False)


def log_case_names() -> list[str]:
    return sorted(path.name for path in LOG_CASES_DIR.iterdir() if path.is_dir())


def load_log_case(name: str) -> LogCase:
    root = LOG_CASES_DIR / name
    payload = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    return LogCase(name=name, root=root, jobs=parse_jobs(payload), expected=expected)


def select_case_jobs(case: LogCase, limit: int = 3) -> list[FailedJob]:
    return select_failed_jobs(make_run_ref(), case.jobs, limit)


def case_excerpts(
    case: LogCase, counter: TokenCounter, config: ExcerptConfig | None = None
) -> list[LogExcerpt]:
    failed = select_case_jobs(case)
    logs = [(job, case.raw_log(job.job.job_id)) for job in failed]
    return build_excerpts(logs, counter, config or ExcerptConfig())


def render_excerpts(excerpts: Sequence[LogExcerpt]) -> str:
    """A readable dump of excerpts, used by the golden files."""
    out: list[str] = []
    for excerpt in excerpts:
        out.append(
            f"# job {excerpt.job.job_id} {excerpt.job.job_name!r} · step {excerpt.step_name!r} · "
            f"{excerpt.token_count} tokens · truncated={excerpt.truncated} · "
            f"redactions={excerpt.redactions} · errors={excerpt.error_lines}"
        )
        gaps = dict(excerpt.omitted)
        if 1 in gaps:
            out.append(f"… lines 1-{gaps[1]} omitted …")
        for line in excerpt.lines:
            out.append(render_line(line))
            if line.number + 1 in gaps:
                start = line.number + 1
                out.append(f"… lines {start}-{gaps[start]} omitted …")
        out.append("")
    return "\n".join(out)


def check_golden(path: Path, actual: str) -> None:
    """Compare with a golden file; ``UPDATE_GOLDEN=1`` rewrites it instead."""
    if UPDATE_GOLDEN or not path.exists():
        path.write_text(actual, encoding="utf-8", newline="\n")
        if not UPDATE_GOLDEN:
            raise AssertionError(f"golden file {path.name} was missing and has been written")
        return
    assert actual == path.read_text(encoding="utf-8"), f"{path} differs (UPDATE_GOLDEN=1 rewrites)"
