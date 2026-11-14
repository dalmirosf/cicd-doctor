"""``python -m evals capture``: turn a run of the lab repo into an eval case (SPEC §11.1, D-61).

It calls the GitHub CLI (``gh``, with the user's own login; no token passes through this code),
downloads the jobs of the run attempt and the log of the failed job, strips terminal escape
sequences from the log, redacts it, finds the evidence lines from the lab manifest's patterns,
and writes ``evals/cases/lab/<id>/``.

The log is untrusted data: it is stripped of terminal controls and redacted, written to disk,
never executed, and never printed. Real logs carry ANSI colours, so ``gh api`` refuses to output
them unless it is given ``--allow-escape-sequences``; the escape sequences are removed before
anything else touches the text (D-62).
"""

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cicd_doctor.adapters.github.jobs import parse_jobs
from cicd_doctor.core.logs import select_failed_jobs, strip_terminal_controls
from cicd_doctor.core.logs.parse import parse_log, slice_step
from cicd_doctor.core.models import Category, FailedJob, RunRef, WorkflowJob
from cicd_doctor.core.redaction import redact_text
from cicd_doctor.core.signature import normalize_job_name
from evals.runner.cases import DEFAULT_CASES_DIR

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "lab" / "cases.yaml"
GH_TIMEOUT_S = 120
# Only these fields of the jobs API are kept in jobs.json (no runner names, URLs or user data).
_JOB_FIELDS = (
    "id",
    "run_id",
    "run_attempt",
    "workflow_name",
    "name",
    "status",
    "conclusion",
    "started_at",
    "completed_at",
    "labels",
)
_STEP_FIELDS = ("name", "status", "conclusion", "number", "started_at", "completed_at")

Gh = Callable[[Sequence[str]], bytes]


class CaptureError(Exception):
    """The run cannot be captured (no failed job, unknown case, gh failed)."""


class LabCase(BaseModel):
    """One entry of ``lab/cases.yaml``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workflow: str
    job: str  # the job's name in the workflow (matrix suffix ignored)
    category: Category
    cause: str
    fix: str
    evidence: list[str] = Field(default_factory=list)  # regexes over the failed step's lines
    flaky: bool = False
    tags: list[str] = Field(default_factory=list)
    captures: int = 1  # how many runs to capture (2 for the signature stability check)


class LabManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[LabCase]

    def case(self, case_id: str) -> LabCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise CaptureError(f"case {case_id!r} is not in the lab manifest")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> LabManifest:
    return LabManifest.model_validate(yaml.safe_load(path.read_text("utf-8")))


def run_gh(args: Sequence[str]) -> bytes:
    """Run ``gh`` with the user's login and return its stdout."""
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, check=True, timeout=GH_TIMEOUT_S
        )
    except FileNotFoundError:
        raise CaptureError("the GitHub CLI (gh) is not installed or not on PATH") from None
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CaptureError(f"gh {args[0]} failed: {message[-1] if message else exc}") from None
    return result.stdout


def fetch_job_log(gh: Gh, repo: str, job_id: int) -> str:
    """The log of a job, without terminal escape sequences or other control characters.

    ``--allow-escape-sequences`` is needed because job logs carry ANSI colours and ``gh api``
    refuses to output them otherwise. The bytes go to a pipe and are never shown on a terminal;
    the escape sequences are stripped here, before redaction, so a colour code cannot split a
    secret and hide it from the redaction patterns (D-62).
    """
    raw = gh(["api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"])
    return strip_terminal_controls(raw.decode("utf-8", errors="replace"))


@dataclass(frozen=True)
class Captured:
    path: Path
    job_id: int
    lines: int
    redactions: int
    evidence_lines: list[int]
    missing_evidence: list[str]  # manifest patterns that matched no line


def capture(
    repo: str,
    run_id: int,
    case_id: str,
    *,
    attempt: int = 1,
    manifest: LabManifest | None = None,
    out_dir: Path = DEFAULT_CASES_DIR / "lab",
    gh: Gh = run_gh,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Captured:
    lab = (manifest or load_manifest()).case(case_id)
    payload = json.loads(
        gh(["api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"])
    )
    jobs = parse_jobs(payload)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    failed = _failed_job(repo, run_id, attempt, jobs, lab)
    redacted = redact_text(fetch_job_log(gh, repo, failed.job.job_id))
    evidence, missing = find_evidence(redacted.text, failed, lab.evidence)

    case_dir = _next_case_dir(out_dir, lab.id)
    case_dir.mkdir(parents=True)
    (case_dir / "log.txt").write_text(redacted.text, encoding="utf-8", newline="")
    (case_dir / "jobs.json").write_text(
        json.dumps(trim_jobs_payload(payload), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    case_yaml = {
        "id": case_dir.name,
        "source": "lab",
        "repo": repo,
        "run_url": run_url,
        "job_id": failed.job.job_id,
        "lab_case": lab.id,
        "expected": {
            "category": lab.category.value,
            "cause": lab.cause,
            "evidence_lines": evidence,
        },
        "fix": {"summary": lab.fix},
        "flaky": lab.flaky,
        "tags": lab.tags,
        "captured_at": now().isoformat(timespec="seconds"),
    }
    (case_dir / "case.yaml").write_text(
        yaml.safe_dump(case_yaml, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return Captured(
        path=case_dir,
        job_id=failed.job.job_id,
        lines=redacted.text.count("\n"),
        redactions=redacted.count,
        evidence_lines=evidence,
        missing_evidence=missing,
    )


def _failed_job(
    repo: str, run_id: int, attempt: int, jobs: Sequence[WorkflowJob], lab: LabCase
) -> FailedJob:
    run = RunRef(
        installation_id=0,
        repo_id=0,
        repo_full_name=repo,
        run_id=run_id,
        run_attempt=attempt,
        workflow_id=0,
        workflow_name=lab.workflow,
        head_sha="0" * 40,
        head_branch="main",
        head_repo_full_name=repo,
        event="workflow_dispatch",
        is_fork=False,
        pr_number=None,
    )
    failed = select_failed_jobs(run, jobs, limit=max(1, len(jobs)))
    if not failed:
        raise CaptureError(f"run {run_id} attempt {attempt} has no failed or timed-out job")
    wanted = normalize_job_name(lab.job)
    for job in failed:
        if normalize_job_name(job.job.job_name) == wanted:
            return job
    names = ", ".join(sorted({job.job.job_name for job in failed}))
    raise CaptureError(f"no failed job named {lab.job!r} in the run (failed: {names})")


def find_evidence(
    text: str, job: FailedJob, patterns: Sequence[str]
) -> tuple[list[int], list[str]]:
    """Line numbers of the failed step (plus its ``##[error]`` lines) that match each pattern.

    Each pattern contributes its first matching line. Returns the sorted line numbers and the
    patterns that matched nothing.
    """
    lines = slice_step(parse_log(text), job.step_started_at, job.step_completed_at)
    found: set[int] = set()
    missing: list[str] = []
    for pattern in patterns:
        regex = re.compile(pattern)
        number = next((line.number for line in lines if regex.search(line.content)), None)
        if number is None:
            missing.append(pattern)
        else:
            found.add(number)
    return sorted(found), missing


def trim_jobs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the pipeline reads (D-56: no raw GitHub JSON beyond what is needed)."""
    jobs = []
    for job in payload.get("jobs", []):
        trimmed = {key: job.get(key) for key in _JOB_FIELDS if key in job}
        trimmed["steps"] = [
            {key: step.get(key) for key in _STEP_FIELDS if key in step}
            for step in job.get("steps") or []
        ]
        jobs.append(trimmed)
    return {"total_count": len(jobs), "jobs": jobs}


def _next_case_dir(out_dir: Path, lab_id: str) -> Path:
    number = 1
    while (out_dir / f"lab-{lab_id}-{number:02d}").exists():
        number += 1
    return out_dir / f"lab-{lab_id}-{number:02d}"


RUN_LIST_LIMIT = 30
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})


def captured_runs(out_dir: Path) -> dict[str, list[str]]:
    """Lab case id -> run URLs already captured under ``out_dir``."""
    runs: dict[str, list[str]] = {}
    for case_yaml in sorted(out_dir.glob("*/case.yaml")):
        data = yaml.safe_load(case_yaml.read_text("utf-8")) or {}
        if data.get("lab_case") and data.get("run_url"):
            runs.setdefault(data["lab_case"], []).append(data["run_url"])
    return runs


def capture_all(
    repo: str,
    *,
    manifest: LabManifest | None = None,
    only: Sequence[str] = (),
    out_dir: Path = DEFAULT_CASES_DIR / "lab",
    gh: Gh = run_gh,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[list[Captured], list[str]]:
    """Capture the latest failed runs of every lab case until each has ``captures`` of them.

    Runs already captured are skipped, so it can be re-run after more workflows finish.
    Returns the new captures and one note per case that still lacks runs.
    """
    lab = manifest or load_manifest()
    done = captured_runs(out_dir)
    new: list[Captured] = []
    notes: list[str] = []
    for case in lab.cases:
        if only and case.id not in only:
            continue
        have = list(done.get(case.id, []))
        if len(have) >= case.captures:
            continue
        listing = gh(
            [
                "run", "list", "-R", repo, "--workflow", case.workflow,
                "--limit", str(RUN_LIST_LIMIT), "--json", "databaseId,attempt,conclusion",
            ]
        )  # fmt: skip
        runs = json.loads(listing)
        cancelled = sum(run.get("conclusion") == "cancelled" for run in runs)
        for run in runs:
            if len(have) >= case.captures:
                break
            if run.get("conclusion") not in FAILED_CONCLUSIONS:
                continue
            run_id, attempt = int(run["databaseId"]), int(run.get("attempt") or 1)
            url = f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}"
            if url in have:
                continue
            captured = capture(
                repo, run_id, case.id, attempt=attempt, manifest=lab, out_dir=out_dir, gh=gh,
                now=now,
            )  # fmt: skip
            new.append(captured)
            have.append(url)
        if len(have) < case.captures:
            missing = f"{case.id}: {len(have)} of {case.captures} captures"
            if cancelled and not have:
                # GitHub ends a job that hits its timeout-minutes as `cancelled`, like a manual
                # cancellation, and the pipeline only diagnoses failure and timed_out (SPEC §14).
                notes.append(
                    f"{missing}; its {cancelled} run(s) ended cancelled, which capture does not "
                    f"take (a job that hits timeout-minutes ends cancelled, SPEC §14), so running "
                    f"{case.workflow} again will not help"
                )
            else:
                notes.append(f"{missing}; run {case.workflow} again")
    return new, notes
