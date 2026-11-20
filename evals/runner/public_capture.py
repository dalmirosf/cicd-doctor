"""``python -m evals capture-public``: turn the entries of the manifest into public cases (D-101).

The manifest (``evals/public-sources.yaml``) is the only source of truth of what is captured: the
repository, the run, the job, the fixing commit, the label and who made it. This module never picks
a failure by itself. For each entry, in this order and stopping at the first reason to discard:

1. the licence of the repository, before any log is downloaded;
2. the run (it failed, its workflow is not excluded) and its jobs (one failed step, a log);
3. the fix (the green run and, for a later commit, which files it touched: names and counts only);
4. the log: strip escapes (D-62) -> trim to the failed step -> ``redact_text`` (D-59) -> the capture
   pass (e-mails and logins) -> the independent scanner, which must find nothing.

A case is written only when everything passes, and nothing is written for a discarded one. The log
is untrusted third-party data: it is stripped, redacted, written to disk and never executed or
printed. The output has ids, counts and reasons from a fixed set, never log text.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cicd_doctor.adapters.github.jobs import parse_jobs
from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.analysis import analyze_jobs
from cicd_doctor.core.logs import failed_step, select_failed_jobs
from cicd_doctor.core.logs.parse import RawLine, parse_log, slice_step
from cicd_doctor.core.models import Category, FailedJob, LogExcerpt, RawLog, RunRef, WorkflowJob
from cicd_doctor.core.ports import TokenCounter
from cicd_doctor.core.redaction import redact_text
from cicd_doctor.core.suggestions import has_hidden_controls
from evals.runner.capture import CaptureError, Gh, fetch_job_log, find_evidence, trim_jobs_payload
from evals.runner.cases import DEFAULT_CASES_DIR, CaseFile
from evals.runner.public_common import (
    ALLOWED_LICENSES,
    MAX_FIX_JSON_BYTES,
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    BudgetExhaustedError,
    is_gone,
    license_problem,
    read_json,
    repo_license,
    run_url,
    valid_case_id,
    valid_repo,
)
from evals.runner.public_scan import capture_pass, scan, summarize

DEFAULT_PUBLIC_MANIFEST = Path(__file__).resolve().parents[1] / "public-sources.yaml"
PUBLIC_DIR = DEFAULT_CASES_DIR / "public"
SOURCES_FILE = "SOURCES.md"
NOTICE_FILE = "NOTICE"
FIX_FILE = "fix.json"
SET_UP_STEP = "Set up job"
# How many lines before and after the failed step a trimmed log keeps, widened one rung at a time
# until the excerpt is the same as the one of the whole log (D-101).
TRIM_LADDER: tuple[tuple[int, int], ...] = ((50, 20), (150, 50), (400, 100), (800, 200))
MAX_FIX_FILES = 10
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----")
_SHA = re.compile(r"^[0-9a-f]{40}$")


# --- the manifest -------------------------------------------------------------------------


class PublicCase(BaseModel):
    """One labelled failure of a public repository."""

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: int
    attempt: int = 1
    job_id: int | None = None  # default: the lowest id among the failed jobs (matrix rule)
    pairing: Literal["H1", "H2"]
    fix_sha: str
    fix_run_id: int | None = None  # H1: the green run of the fixing commit
    fix_attempt: int | None = None  # H2: the later green attempt of the same run
    category: Category
    cause: str = Field(max_length=200)
    fix_summary: str = Field(max_length=200)
    evidence: list[str] = Field(default_factory=list)  # regexes over the failed step's lines
    flaky: bool = False
    tags: list[str] = Field(default_factory=list)
    labeled_by: str
    label_basis: Literal["fix", "log"]

    @model_validator(mode="after")
    def _check(self) -> "PublicCase":
        if not valid_case_id(self.id):
            raise ValueError(f"case id {self.id!r} is not a safe directory name")
        if not _SHA.match(self.fix_sha):
            raise ValueError(f"{self.id}: fix_sha must be a full 40-character sha")
        if self.pairing == "H1" and self.fix_run_id is None:
            raise ValueError(f"{self.id}: an H1 case needs fix_run_id")
        if self.pairing == "H2" and (self.fix_attempt is None or self.fix_attempt <= self.attempt):
            raise ValueError(f"{self.id}: an H2 case needs fix_attempt greater than attempt")
        return self


class PublicRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    license: str  # what the owner expects; the API must say the same
    exclude_workflows: list[str] = Field(default_factory=list)  # substrings of workflow names
    cases: list[PublicCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "PublicRepo":
        if not valid_repo(self.repo):
            raise ValueError(f"{self.repo!r} is not an owner/name repository")
        if self.license not in ALLOWED_LICENSES:
            raise ValueError(f"{self.repo}: licence {self.license!r} is not allowed")
        return self


class PublicManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    repos: list[PublicRepo]

    @model_validator(mode="after")
    def _unique(self) -> "PublicManifest":
        ids = [case.id for repo in self.repos for case in repo.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids in the manifest must be unique")
        return self


def load_public_manifest(path: Path = DEFAULT_PUBLIC_MANIFEST) -> PublicManifest:
    return PublicManifest.model_validate(yaml.safe_load(path.read_text("utf-8")))


# --- the result ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Written:
    id: str
    path: Path
    job_id: int
    lines: int
    bytes: int
    redactions: int
    capture_replacements: int
    trimmed: bool
    evidence_lines: list[int]
    missing_evidence: list[str]


@dataclass
class PublicReport:
    written: list[Written] = field(default_factory=list)
    discarded: list[tuple[str, str]] = field(default_factory=list)  # (case id, reason)
    stopped: str | None = None  # the budget ran out: the rest was not tried


class DiscardError(Exception):
    """A reason to leave a case out. The reason comes from a fixed vocabulary: never log text."""


# --- trimming -----------------------------------------------------------------------------


def _internal_gaps(excerpt: LogExcerpt) -> list[int]:
    if not excerpt.lines:
        return []
    first, last = excerpt.lines[0].number, excerpt.lines[-1].number
    return [b - a + 1 for a, b in excerpt.omitted if a > first and b < last]


def _view(excerpt: LogExcerpt) -> tuple[Any, ...]:
    """What an excerpt says, independent of where its lines sit in the log: the texts, the step,
    the budget, which kept lines are errors and the size of the gaps between them."""
    numbers = [line.number for line in excerpt.lines]
    errors = [numbers.index(n) for n in excerpt.error_lines if n in numbers]
    return (
        excerpt.step_name,
        [line.text for line in excerpt.lines],
        errors,
        _internal_gaps(excerpt),
        excerpt.token_count,
        excerpt.truncated,
        excerpt.redactions,
    )


def _excerpt(job: FailedJob, text: str, counter: TokenCounter) -> LogExcerpt:
    return analyze_jobs([(job, RawLog(text=text, truncated=False))], counter)[0].excerpt


def equivalent(job: FailedJob, full: str, trimmed: str, counter: TokenCounter) -> bool:
    """The excerpt of the trimmed log says what the excerpt of the whole log says (D-101)."""
    return _view(_excerpt(job, full, counter)) == _view(_excerpt(job, trimmed, counter))


def _fits(text: str, lines: int) -> bool:
    return len(text.encode("utf-8")) <= MAX_LOG_BYTES and lines <= MAX_LOG_LINES


def _pem_balanced(text: str) -> bool:
    return len(_PEM_BEGIN.findall(text)) == len(_PEM_END.findall(text))


def trim_log(text: str, job: FailedJob, counter: TokenCounter) -> tuple[str, bool]:
    """The log to store and whether it was cut.

    A log within the limits is kept whole. A larger one is cut to the failed step plus the least
    context that gives the same excerpt as the whole log; no rung that fits and is equivalent means
    the case is discarded: it would measure an app that does not exist.
    """
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    if _fits(text, len(parts)):
        return text, False
    lines = parse_log(text)
    if job.step_started_at is None or job.step_completed_at is None:
        raise DiscardError("log too large and the failed step cannot be located")
    # The lines of the step by time alone: with their text emptied no line is an ``##[error]``
    # one, so an error far from the step does not stretch the cut (the check below notices it).
    window = slice_step(
        [RawLine(line.number, line.second, "") for line in lines],
        job.step_started_at,
        job.step_completed_at,
    )
    if not window or len(window) == len(lines):
        raise DiscardError("log too large and the failed step cannot be located")
    first, last = window[0].number, window[-1].number
    fitted = cut_key = False
    for before, after in TRIM_LADDER:
        low, high = max(1, first - before), min(len(parts), last + after)
        candidate = "\n".join(parts[low - 1 : high]) + "\n"
        if not _fits(candidate, high - low + 1):
            break  # a wider cut will not fit either
        fitted = True
        if not _pem_balanced(candidate):
            cut_key = True
            continue
        if equivalent(job, text, candidate, counter):
            return candidate, True
    if cut_key:
        raise DiscardError("trim would cut a private key block")
    raise DiscardError("trim not equivalent" if fitted else "log too large")


# --- the case -----------------------------------------------------------------------------


def _stub_run(repo: str, run_id: int, attempt: int, workflow: str) -> RunRef:
    return RunRef(
        installation_id=0,
        repo_id=0,
        repo_full_name=repo,
        run_id=run_id,
        run_attempt=attempt,
        workflow_id=0,
        workflow_name=workflow,
        head_sha="0" * 40,
        head_branch="main",
        head_repo_full_name=repo,
        event="workflow_dispatch",
        is_fork=False,
        pr_number=None,
    )


def pick_job(
    repo: str,
    run_id: int,
    attempt: int,
    jobs: Sequence[WorkflowJob],
    wanted: int | None,
) -> FailedJob:
    """The failed job of a case under the matrix rule (D-101).

    Every failed job must fail at the same step (the usual matrix: one cause, many cells), and the
    case takes the lowest job id. Failures at different steps are different problems: discarded.
    A job that failed in ``Set up job`` never ran, so it has no log.
    """
    failed = select_failed_jobs(
        _stub_run(repo, run_id, attempt, "-"), jobs, limit=max(1, len(jobs))
    )
    if not failed:
        raise DiscardError("no failed job")
    steps = [failed_step(_job(jobs, item.job.job_id)) for item in failed]
    names = {step.name if step else None for step in steps}
    if len(names) > 1:
        raise DiscardError("failed jobs fail at different steps")
    if SET_UP_STEP in names or None in names:
        raise DiscardError("the failed job never started its steps")
    failed.sort(key=lambda item: item.job.job_id)
    if wanted is None:
        return failed[0]
    for item in failed:
        if item.job.job_id == wanted:
            return item
    raise DiscardError("job_id is not a failed job of the attempt")


def _job(jobs: Sequence[WorkflowJob], job_id: int) -> WorkflowJob:
    return next(job for job in jobs if job.job_id == job_id)


def fix_record(
    case: PublicCase, repo: str, run_sha: str, gh: Gh, failed_at: datetime
) -> dict[str, Any]:
    """``fix.json``: the fixing commit, the files it touched (names and counts) and the label's
    sentence. No diff text is read or stored (D-101)."""
    if case.pairing == "H2":
        green = read_json(
            gh, f"repos/{repo}/actions/runs/{case.run_id}/attempts/{case.fix_attempt}"
        )
        _check_green(green, case.fix_sha, failed_at)
        files: list[dict[str, Any]] = []
        commits = 0
        total = 0
    else:
        green = read_json(gh, f"repos/{repo}/actions/runs/{case.fix_run_id}")
        _check_green(green, case.fix_sha, failed_at)
        compare = read_json(gh, f"repos/{repo}/compare/{run_sha}...{case.fix_sha}")
        raw_files = compare.get("files") if isinstance(compare, dict) else None
        if not isinstance(raw_files, list) or not raw_files:
            raise DiscardError("the fix touched no files")
        total = len(raw_files)
        if total > MAX_FIX_FILES:
            raise DiscardError("the fix touched too many files")
        files = []
        for item in raw_files:
            path = item.get("filename") if isinstance(item, dict) else None
            if not isinstance(path, str) or len(path) > 200 or has_hidden_controls(path):
                raise DiscardError("a file name of the fix is not storable")
            files.append(
                {
                    "path": path,
                    "additions": int(item.get("additions", 0)),
                    "deletions": int(item.get("deletions", 0)),
                }
            )
        commits = int(compare.get("total_commits", 0))
    record = {
        "sha": case.fix_sha,
        "pairing": case.pairing,
        "commits": commits,
        "files_total": total,
        "files": files,
        "summary": case.fix_summary,
    }
    if len(json.dumps(record, indent=2).encode("utf-8")) > MAX_FIX_JSON_BYTES:
        raise DiscardError("fix.json too large")
    return record


def _check_green(green: object, fix_sha: str, failed_at: datetime) -> None:
    if not isinstance(green, dict) or green.get("conclusion") != "success":
        raise DiscardError("the fix run is not green")
    if green.get("head_sha") != fix_sha:
        raise DiscardError("the fix run is for another commit")
    done = _parse_time(green.get("updated_at") or green.get("run_started_at"))
    if done is not None and done < failed_at:
        raise DiscardError("the fix run is older than the failure")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_log(text: str, job: FailedJob, counter: TokenCounter) -> tuple[str, bool, int, int]:
    """Trim, redact, capture pass, scan. Returns the text, whether it was trimmed and the counts."""
    trimmed, was_trimmed = trim_log(text, job, counter)
    redacted = redact_text(trimmed)
    passed = capture_pass(redacted.text)
    findings = scan(passed.text)
    if findings:
        raise DiscardError(summarize(findings))
    if not _fits(passed.text, passed.text.count("\n")):
        raise DiscardError("log too large")
    return passed.text, was_trimmed, redacted.count, passed.emails + passed.logins


def build_case(
    repo: PublicRepo,
    case: PublicCase,
    gh: Gh,
    counter: TokenCounter,
    now: datetime,
) -> tuple[dict[str, str], Written]:
    """The files of a case directory as text, or a ``DiscardError``. Nothing touches the disk."""
    run = read_json(gh, f"repos/{repo.repo}/actions/runs/{case.run_id}/attempts/{case.attempt}")
    if not isinstance(run, dict) or run.get("conclusion") not in ("failure", "timed_out"):
        raise DiscardError("the run did not fail")
    workflow = str(run.get("name") or "").lower()
    if any(term.lower() in workflow for term in repo.exclude_workflows):
        raise DiscardError("workflow is on the exclusion list")
    run_sha = str(run.get("head_sha") or "")
    if case.pairing == "H2" and run_sha != case.fix_sha:
        raise DiscardError("an H2 case must have the same commit as its fix")
    payload = read_json(
        gh,
        f"repos/{repo.repo}/actions/runs/{case.run_id}/attempts/{case.attempt}/jobs?per_page=100",
    )
    jobs = parse_jobs(payload)
    failed = pick_job(repo.repo, case.run_id, case.attempt, jobs, case.job_id)
    fix = fix_record(case, repo.repo, run_sha, gh, failed.completed_at)

    try:
        text = fetch_job_log(gh, repo.repo, failed.job.job_id)
    except CaptureError as error:
        if is_gone(error):
            raise DiscardError("log expired or missing (404/410)") from None
        raise DiscardError("gh failed reading the log") from None
    log, trimmed, redactions, replaced = _clean_log(text, failed, counter)

    jobs_json = trim_jobs_payload(payload)
    jobs_json["jobs"] = [job for job in jobs_json["jobs"] if job.get("id") == failed.job.job_id]
    jobs_json["total_count"] = len(jobs_json["jobs"])
    jobs_text = json.dumps(jobs_json, indent=2) + "\n"
    fix_text = json.dumps(fix, indent=2) + "\n"
    label_text = "\n".join([case.cause, case.fix_summary, case.labeled_by, *case.tags])
    findings = scan(jobs_text + fix_text + label_text)
    if findings:
        raise DiscardError("the metadata " + summarize(findings))

    evidence, missing = find_evidence(log, failed, case.evidence)
    meta = CaseFile.model_validate(
        {
            "id": case.id,
            "source": "public",
            "repo": repo.repo,
            "run_url": run_url(repo.repo, case.run_id, case.attempt),
            "job_id": failed.job.job_id,
            "expected": {
                "category": case.category.value,
                "cause": case.cause,
                "evidence_lines": evidence,
            },
            "fix": {"commit": case.fix_sha, "summary": case.fix_summary},
            "flaky": case.flaky,
            "tags": case.tags,
            "captured_at": now.isoformat(timespec="seconds"),
            "provenance": {
                "repo": repo.repo,
                "license": repo.license,
                "run_url": run_url(repo.repo, case.run_id, case.attempt),
                "fix_sha": case.fix_sha,
                "fix_run_url": _fix_run_url(repo.repo, case),
                "occurred_at": failed.completed_at.astimezone(UTC).isoformat(timespec="seconds"),
                "pairing": case.pairing,
                "labeled_by": case.labeled_by,
                "label_basis": case.label_basis,
            },
        }
    )
    case_yaml = yaml.safe_dump(
        meta.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    files = {
        "case.yaml": case_yaml,
        "log.txt": log,
        "jobs.json": jobs_text,
        FIX_FILE: fix_text,
    }
    written = Written(
        id=case.id,
        path=Path(case.id),
        job_id=failed.job.job_id,
        lines=log.count("\n"),
        bytes=len(log.encode("utf-8")),
        redactions=redactions,
        capture_replacements=replaced,
        trimmed=trimmed,
        evidence_lines=evidence,
        missing_evidence=missing,
    )
    return files, written


def _fix_run_url(repo: str, case: PublicCase) -> str:
    if case.pairing == "H2":
        return run_url(repo, case.run_id, case.fix_attempt or case.attempt)
    return f"https://github.com/{repo}/actions/runs/{case.fix_run_id}"


def capture_public(
    manifest: PublicManifest,
    *,
    gh: Gh,
    out_dir: Path = PUBLIC_DIR,
    counter: TokenCounter | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    only: Sequence[str] = (),
) -> PublicReport:
    """Capture every entry of the manifest (or the ``only`` ids). One bad entry never stops the
    batch; running out of API budget does, and says so in ``stopped``."""
    report = PublicReport()
    tokens = counter or CharTokenCounter()
    try:
        for repo in manifest.repos:
            entries = [case for case in repo.cases if not only or case.id in only]
            if not entries:
                continue
            try:
                spdx, private = repo_license(gh, repo.repo)
            except CaptureError:
                report.discarded += [
                    (case.id, "gh failed reading the repository") for case in entries
                ]
                continue
            problem = license_problem(spdx, private, repo.license)
            if problem:  # before any log is downloaded
                report.discarded += [(case.id, problem) for case in entries]
                continue
            for case in entries:
                _one(repo, case, gh, tokens, now(), out_dir, report)
    except BudgetExhaustedError as stop:
        report.stopped = str(stop)
    if report.written:
        write_sources(out_dir)
    return report


def _one(
    repo: PublicRepo,
    case: PublicCase,
    gh: Gh,
    counter: TokenCounter,
    now: datetime,
    out_dir: Path,
    report: PublicReport,
) -> None:
    target = out_dir / case.id
    if target.exists():
        report.discarded.append((case.id, "already captured"))
        return
    try:
        files, written = build_case(repo, case, gh, counter, now)
    except DiscardError as reason:
        report.discarded.append((case.id, str(reason)))
        return
    except CaptureError:
        report.discarded.append((case.id, "gh failed"))
        return
    target.mkdir(parents=True)
    for name, content in files.items():
        (target / name).write_text(content, encoding="utf-8", newline="")
    report.written.append(replace(written, path=target))


# --- SOURCES.md, NOTICE and the check -----------------------------------------------------


def _public_cases(public_dir: Path) -> list[CaseFile]:
    """The cases of the directory that have a ``provenance`` (the check reports the others)."""
    cases = _all_cases(public_dir)
    return [case for case in cases if case.provenance is not None]


def _all_cases(public_dir: Path) -> list[CaseFile]:
    if not public_dir.is_dir():
        return []
    return [
        CaseFile.model_validate(yaml.safe_load(path.read_text("utf-8")))
        for path in sorted(public_dir.glob("*/case.yaml"))
    ]


def render_sources(cases: Sequence[CaseFile]) -> str:
    lines = [
        "# Public cases: sources",
        "",
        "Generated by `python -m evals capture-public`; do not edit. Each case is an excerpt of a",
        "public GitHub Actions log, redacted, with the label of the person named below. The",
        "licence is the one the repository declared when the case was captured (see `NOTICE`).",
        "",
        "| Case | Repository | Licence | Failed run | Fix | Pairing | Occurred | Labelled by |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in sorted(cases, key=lambda c: c.id):
        p = case.provenance
        assert p is not None
        lines.append(
            f"| {case.id} | {p.repo} | {p.license} | {p.run_url} | {p.fix_sha[:12]} | {p.pairing} "
            f"| {p.occurred_at.date().isoformat()} | {p.labeled_by} ({p.label_basis}) |"
        )
    return "\n".join(lines) + "\n"


def render_notice(cases: Sequence[CaseFile]) -> str:
    repos: dict[str, str] = {}
    for case in cases:
        assert case.provenance is not None
        repos[case.provenance.repo] = case.provenance.license
    lines = [
        "The files under this directory contain excerpts of public GitHub Actions logs from the",
        "repositories below, each under the licence it declares. The excerpts are redacted and",
        "trimmed; copyright and trademarks remain with their owners. A maintainer who wants a case",
        "removed can ask and it will be deleted.",
        "",
    ]
    lines += [f"- {repo}: {repos[repo]}" for repo in sorted(repos)]
    return "\n".join(lines) + "\n"


def write_sources(public_dir: Path) -> None:
    cases = _public_cases(public_dir)
    (public_dir / SOURCES_FILE).write_text(render_sources(cases), encoding="utf-8", newline="\n")
    (public_dir / NOTICE_FILE).write_text(render_notice(cases), encoding="utf-8", newline="\n")


def check_public_cases(public_dir: Path) -> list[str]:
    """Problems of the public cases on disk: a case missing from SOURCES.md or NOTICE, a licence
    that is not allowed, a stale SOURCES.md, a log the scanner does not accept, an oversized file.
    Empty when all is well (or when there are no public cases)."""
    problems: list[str] = []
    everything = _all_cases(public_dir)
    problems += [f"{c.id}: no provenance" for c in everything if c.provenance is None]
    cases = _public_cases(public_dir)
    if not cases:
        return problems
    sources = (
        (public_dir / SOURCES_FILE).read_text("utf-8")
        if (public_dir / SOURCES_FILE).is_file()
        else ""
    )
    notice = (
        (public_dir / NOTICE_FILE).read_text("utf-8")
        if (public_dir / NOTICE_FILE).is_file()
        else ""
    )
    for case in cases:
        provenance = case.provenance
        assert provenance is not None
        if provenance.license not in ALLOWED_LICENSES:
            problems.append(f"{case.id}: licence {provenance.license} is not allowed")
        if f"| {case.id} |" not in sources:
            problems.append(f"{case.id}: missing from {SOURCES_FILE}")
        if provenance.repo not in notice:
            problems.append(f"{case.id}: repository missing from {NOTICE_FILE}")
        root = public_dir / case.id
        log = (
            (root / "log.txt").read_text("utf-8", errors="replace")
            if (root / "log.txt").is_file()
            else ""
        )
        if not log:
            problems.append(f"{case.id}: no log.txt")
        elif scan(log):
            problems.append(f"{case.id}: the scanner finds something in log.txt")
        if len(log.encode("utf-8")) > MAX_LOG_BYTES or log.count("\n") > MAX_LOG_LINES:
            problems.append(f"{case.id}: log.txt is over the limits")
        fix_path = root / FIX_FILE
        if not fix_path.is_file():
            problems.append(f"{case.id}: no {FIX_FILE}")
        elif fix_path.stat().st_size > MAX_FIX_JSON_BYTES:
            problems.append(f"{case.id}: {FIX_FILE} is over {MAX_FIX_JSON_BYTES} bytes")
    if sources != render_sources(cases) or notice != render_notice(cases):
        problems.append(f"{SOURCES_FILE} or {NOTICE_FILE} is stale: run capture-public again")
    return problems
