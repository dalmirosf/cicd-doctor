"""``python -m evals discover``: list the failed runs of a public repository and pair each one with
the run that proves its fix (D-101). Read only: every call is a ``GET`` through the injected ``gh``.

Two pairings, and nothing is a case until the owner labels it (D-102):

- **H1**, a later commit fixed it: the next conclusive run of the same workflow and branch is green,
  with another head commit, at most 3 commits, 1 to 10 files and 7 days away. If the next
  conclusive run failed too, this failure was superseded and is left out (the later one is the
  candidate); cancelled and skipped runs in between are ignored.
- **H2**, a re-run fixed it (a flaky candidate): the same commit has a later green attempt.

- **H1-pr**, the same as H1 for a pull request from a fork, paired by pull request instead of by
  branch: the PR of the failed head commit (`commits/{sha}/pulls`), its commits
  (`pulls/{n}/commits`), and the runs of the same workflow on the next head commits of that PR
  (at most `MAX_COMMITS`). The same filters apply. A PR whose history no longer has the failed
  commit (a force-push) is discarded. When the commit has no PR (a fork head commit often has
  none), the PR is looked up by the run's head owner and branch (`pulls?head=<owner>:<branch>`) and
  accepted only if its own commits contain the failed commit; the candidate records which lookup
  found it (`pr_lookup`: `commit` or `owner_branch`). A deleted fork is tried the same way.

A matrix is accepted when every failed job fails at the same step, taking the lowest job id;
different steps, a job that never started and workflows on the exclusion list are discarded, each
with its reason."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from cicd_doctor.adapters.github.jobs import parse_jobs
from evals.runner.capture import CaptureError, Gh
from evals.runner.public_capture import DiscardError, pick_job
from evals.runner.public_common import (
    DEFAULT_WORKFLOW_EXCLUSIONS,
    BudgetExhaustedError,
    clean_label,
    is_gone,
    license_problem,
    read_json,
    repo_license,
)

MAX_COMMITS = 3
MAX_FILES = 10
MAX_DAYS = 7
RUN_PAGES = 3
RERUN_PAGES = 2
FAILED_STATUSES = ("failure", "timed_out")
_CONCLUSIVE_FAILURES = frozenset(FAILED_STATUSES)
_NOT_CONCLUSIVE = frozenset({"cancelled", "skipped", "neutral", "stale", "action_required", None})
PULL_PAGES = 3
MAX_BRANCH_PULLS = 5  # pull requests of one owner:branch whose commits are read to confirm
DEFAULT_CANDIDATES = Path("candidates.yaml")


class PrLookup(StrEnum):
    """Which lookup found the pull request of an H1-pr candidate."""

    COMMIT = "commit"  # GET commits/{sha}/pulls
    OWNER_BRANCH = "owner_branch"  # GET pulls?head=<owner>:<branch>, confirmed by its commits


@dataclass
class Discovery:
    repo: str
    license: str | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    api_calls: int = 0
    stopped: str | None = None

    def discard(self, run: dict[str, Any], reason: str) -> None:
        self.discarded.append(
            {
                "run_id": run.get("id"),
                "attempt": run.get("run_attempt"),
                "workflow": clean_label(run.get("name")),
                "reason": reason,
            }
        )


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _created(run: dict[str, Any]) -> datetime:
    return _time(run.get("created_at")) or datetime.min.replace(tzinfo=UTC)


def _excluded(run: dict[str, Any], terms: Sequence[str]) -> bool:
    name = str(run.get("name") or "").lower()
    return any(term.lower() in name for term in terms)


def _runs(gh: Gh, endpoint: str) -> list[dict[str, Any]]:
    data = read_json(gh, endpoint)
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def _since(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%d")


def _candidate_id(repo: str, run_id: object, attempt: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-")
    return f"{slug}-{run_id}-{attempt}"


def discover(
    repo: str,
    *,
    gh: Gh,
    since_days: int = 90,
    limit: int | None = None,
    exclude: Sequence[str] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    calls: Callable[[], int] = lambda: 0,
) -> Discovery:
    """Candidates for one repository, with every discard and its reason."""
    result = Discovery(repo=repo, license=None)
    terms = [*DEFAULT_WORKFLOW_EXCLUSIONS, *exclude]
    since = _since(now(), since_days)
    try:
        spdx, private = repo_license(gh, repo)
        result.license = spdx
        problem = license_problem(spdx, private)
        if problem:
            result.discarded.append(
                {"run_id": None, "attempt": None, "workflow": "", "reason": problem}
            )
            return result
        _h1(result, gh, since, terms, limit)
        if limit is None or len(result.candidates) < limit:
            _h2(result, gh, since, terms, limit)
    except BudgetExhaustedError as stop:
        result.stopped = str(stop)
    except CaptureError:
        result.stopped = "gh failed"
    result.api_calls = calls()
    return result


def _full(result: Discovery, limit: int | None) -> bool:
    return limit is not None and len(result.candidates) >= limit


def _h1(result: Discovery, gh: Gh, since: str, terms: Sequence[str], limit: int | None) -> None:
    failed: list[dict[str, Any]] = []
    for status in FAILED_STATUSES:
        for page in range(1, RUN_PAGES + 1):
            batch = _runs(
                gh,
                f"repos/{result.repo}/actions/runs?status={status}&per_page=100"
                f"&created=>={since}&page={page}",
            )
            failed += batch
            if len(batch) < 100:
                break
    failed.sort(key=_created, reverse=True)
    branch_runs: dict[tuple[object, str], list[dict[str, Any]]] = {}
    pulls = _PullCache()
    for run in failed:
        if _full(result, limit):
            return
        if _excluded(run, terms):
            result.discard(run, "workflow is on the exclusion list")
            continue
        head_repo = run.get("head_repository")
        deleted_fork = "head_repository" in run and head_repo is None
        if deleted_fork or (
            isinstance(head_repo, dict) and head_repo.get("full_name") != result.repo
        ):
            _pair_fork(result, gh, run, pulls)
            continue
        branch = run.get("head_branch")
        if not isinstance(branch, str) or not branch:
            result.discard(run, "no branch")
            continue
        key = (run.get("workflow_id"), branch)
        if key not in branch_runs:
            siblings = _runs(
                gh,
                f"repos/{result.repo}/actions/workflows/{run.get('workflow_id')}/runs"
                f"?branch={quote(branch, safe='')}&per_page=100&created=>={since}",
            )
            branch_runs[key] = sorted(siblings, key=lambda r: (_created(r), r.get("id") or 0))
        _pair(result, gh, run, branch_runs[key])


@dataclass
class _PullCache:
    """What was already read about pull requests, so that two failures of one PR cost it once."""

    by_sha: dict[str, int | None] = field(default_factory=dict)
    by_head: dict[tuple[str, str, str], int | None] = field(default_factory=dict)
    commits: dict[int, list[str] | None] = field(default_factory=dict)
    runs: dict[tuple[object, str], list[dict[str, Any]]] = field(default_factory=dict)


def _pull_of(result: Discovery, gh: Gh, sha: str, cache: _PullCache) -> int | None:
    if sha not in cache.by_sha:
        try:
            data = read_json(gh, f"repos/{result.repo}/commits/{sha}/pulls?per_page=30")
        except CaptureError as error:
            if not is_gone(error):
                raise
            data = []
        found = [
            p
            for p in (data if isinstance(data, list) else [])
            if isinstance(p, dict)
            and isinstance(p.get("number"), int)
            and ((p.get("base") or {}).get("repo") or {}).get("full_name") == result.repo
        ]
        found.sort(key=lambda p: (p.get("head") or {}).get("sha") != sha)  # same head first
        cache.by_sha[sha] = found[0]["number"] if found else None
    return cache.by_sha[sha]


def _head_owner(run: dict[str, Any]) -> str | None:
    head_repo = run.get("head_repository")
    if not isinstance(head_repo, dict):
        return None
    owner = (head_repo.get("owner") or {}).get("login")
    if not isinstance(owner, str) or not owner:
        full = head_repo.get("full_name")
        owner = full.split("/")[0] if isinstance(full, str) and "/" in full else None
    return owner or None


def _pull_of_branch(
    result: Discovery, gh: Gh, run: dict[str, Any], sha: str, cache: _PullCache
) -> int | None:
    """The pull request of ``<owner>:<branch>`` whose own commits contain ``sha``. The branch name
    alone proves nothing (two forks can use the same one): the commits of the PR decide."""
    owner, branch = _head_owner(run), run.get("head_branch")
    if owner is None or not isinstance(branch, str) or not branch:
        return None
    key = (owner, branch, sha)
    if key in cache.by_head:
        return cache.by_head[key]
    found: list[dict[str, Any]] = []
    for page in range(1, PULL_PAGES + 1):
        try:
            data = read_json(
                gh,
                f"repos/{result.repo}/pulls?state=all&head={quote(owner, safe='')}:"
                f"{quote(branch, safe='')}&per_page=100&page={page}",
            )
        except CaptureError as error:
            if not is_gone(error):
                raise
            data = []
        found += [
            p
            for p in (data if isinstance(data, list) else [])
            if isinstance(p, dict)
            and isinstance(p.get("number"), int)
            and ((p.get("base") or {}).get("repo") or {}).get("full_name") == result.repo
        ]
        if not isinstance(data, list) or len(data) < 100:
            break
    found.sort(key=lambda p: (p.get("head") or {}).get("sha") != sha)  # same head first
    number: int | None = None
    for pull in found[:MAX_BRANCH_PULLS]:
        shas = _pull_commits(result, gh, pull["number"], cache)
        if shas is not None and sha in shas:
            number = pull["number"]
            break
    cache.by_head[key] = number
    return number


def _pull_commits(result: Discovery, gh: Gh, number: int, cache: _PullCache) -> list[str] | None:
    if number not in cache.commits:
        try:
            data = read_json(gh, f"repos/{result.repo}/pulls/{number}/commits?per_page=100")
        except CaptureError as error:
            if not is_gone(error):
                raise
            data = None
        shas = (
            [str(c.get("sha")) for c in data if isinstance(c, dict)]
            if isinstance(data, list)
            else []
        )
        cache.commits[number] = shas or None
    return cache.commits[number]


def _pair_fork(result: Discovery, gh: Gh, run: dict[str, Any], cache: _PullCache) -> None:
    """H1-pr: pair a failed run of a fork PR by pull request, not by branch."""
    sha = str(run.get("head_sha") or "")
    if not sha:
        result.discard(run, "no head commit")
        return
    number = _pull_of(result, gh, sha, cache)
    lookup = PrLookup.COMMIT
    if number is None:
        number = _pull_of_branch(result, gh, run, sha, cache)
        lookup = PrLookup.OWNER_BRANCH
    if number is None:
        reason = (
            "fork run: the head repository is gone and no pull request has the commit"
            if run.get("head_repository") is None
            else "fork run: no pull request for the commit"
        )
        result.discard(run, reason)
        return
    shas = _pull_commits(result, gh, number, cache)
    if shas is None or sha not in shas:
        result.discard(run, "fork run: the commit is no longer in the pull request")
        return
    later = shas[shas.index(sha) + 1 :][:MAX_COMMITS]
    if not later:
        result.discard(run, "fork run: no later commit in the pull request")
        return
    siblings = [run]
    for later_sha in later:
        key = (run.get("workflow_id"), later_sha)
        if key not in cache.runs:
            cache.runs[key] = _runs(
                gh,
                f"repos/{result.repo}/actions/workflows/{run.get('workflow_id')}/runs"
                f"?head_sha={later_sha}&per_page=100",
            )
        siblings += cache.runs[key]
    siblings.sort(key=lambda r: (_created(r), r.get("id") or 0))
    _pair(result, gh, run, siblings, pairing="H1-pr", pr=number, lookup=lookup)


def _pair(
    result: Discovery,
    gh: Gh,
    run: dict[str, Any],
    siblings: list[dict[str, Any]],
    *,
    pairing: str = "H1",
    pr: int | None = None,
    lookup: PrLookup | None = None,
) -> None:
    later = [
        r for r in siblings if (_created(r), r.get("id") or 0) > (_created(run), run.get("id") or 0)
    ]
    green: dict[str, Any] | None = None
    for other in later:
        conclusion = other.get("conclusion")
        if conclusion in _NOT_CONCLUSIVE:
            continue  # cancelled, skipped, still running: not an answer
        if conclusion in _CONCLUSIVE_FAILURES:
            result.discard(run, "superseded by a later failure")
            return
        if conclusion == "success":
            green = other
        break
    if green is None:
        result.discard(run, "no green run after it")
        return
    if green.get("head_sha") == run.get("head_sha"):
        result.discard(run, "the green run has the same commit (see H2)")
        return
    days = (_created(green) - _created(run)).total_seconds() / 86400
    if days > MAX_DAYS:
        result.discard(run, f"the green run is {days:.0f} days later")
        return
    compare = read_json(
        gh, f"repos/{result.repo}/compare/{run.get('head_sha')}...{green.get('head_sha')}"
    )
    files = compare.get("files") if isinstance(compare, dict) else None
    commits = compare.get("total_commits") if isinstance(compare, dict) else None
    if not isinstance(files, list) or not isinstance(commits, int):
        result.discard(run, "the comparison has no files or commits")
        return
    if commits > MAX_COMMITS:
        result.discard(run, f"{commits} commits between the runs")
        return
    if not 1 <= len(files) <= MAX_FILES:
        result.discard(run, "no files changed" if not files else f"{len(files)} files changed")
        return
    job = _failed_job(result, gh, run, int(run.get("run_attempt") or 1))
    if job is None:
        return
    result.candidates.append(
        _record(
            result.repo,
            run,
            job,
            attempt=int(run.get("run_attempt") or 1),
            pairing=pairing,
            pr=pr,
            lookup=lookup,
            fix_sha=str(green.get("head_sha")),
            fix_run_id=green.get("id"),
            fix_attempt=None,
            days=days,
            commits=commits,
            files=files,
        )
    )


def _h2(result: Discovery, gh: Gh, since: str, terms: Sequence[str], limit: int | None) -> None:
    greens: list[dict[str, Any]] = []
    for page in range(1, RERUN_PAGES + 1):
        batch = _runs(
            gh,
            f"repos/{result.repo}/actions/runs?status=success&per_page=100"
            f"&created=>={since}&page={page}",
        )
        greens += batch
        if len(batch) < 100:
            break
    seen = {(c["run_id"], c["attempt"]) for c in result.candidates}
    for green in sorted(greens, key=_created, reverse=True):
        attempt = int(green.get("run_attempt") or 1)
        if attempt < 2:
            continue
        if _full(result, limit):
            return
        if _excluded(green, terms):
            result.discard(green, "workflow is on the exclusion list")
            continue
        if (green.get("id"), attempt - 1) in seen:
            continue
        previous = read_json(
            gh, f"repos/{result.repo}/actions/runs/{green.get('id')}/attempts/{attempt - 1}"
        )
        if not isinstance(previous, dict) or previous.get("conclusion") not in _CONCLUSIVE_FAILURES:
            result.discard(green, "the previous attempt did not fail")
            continue
        gap = (_time(green.get("updated_at")) or _created(green)) - (
            _time(previous.get("updated_at")) or _created(previous)
        )
        days = gap.total_seconds() / 86400
        if days > MAX_DAYS:
            result.discard(previous, f"the green attempt is {days:.0f} days later")
            continue
        job = _failed_job(result, gh, previous, attempt - 1)
        if job is None:
            continue
        result.candidates.append(
            _record(
                result.repo,
                previous,
                job,
                attempt=attempt - 1,
                pairing="H2",
                fix_sha=str(green.get("head_sha")),
                fix_run_id=green.get("id"),
                fix_attempt=attempt,
                days=days,
                commits=0,
                files=[],
            )
        )


def _failed_job(
    result: Discovery, gh: Gh, run: dict[str, Any], attempt: int
) -> dict[str, Any] | None:
    payload = read_json(
        gh, f"repos/{result.repo}/actions/runs/{run.get('id')}/attempts/{attempt}/jobs?per_page=100"
    )
    jobs = parse_jobs(payload)
    try:
        failed = pick_job(result.repo, int(run.get("id") or 0), attempt, jobs, None)
    except DiscardError as reason:
        result.discard(run, str(reason))
        return None
    count = sum(1 for job in jobs if job.conclusion in ("failure", "timed_out"))
    return {
        "job_id": failed.job.job_id,
        "job": failed.job.job_name,
        "step": failed.step_name,
        "failed": count,
    }


def _record(
    repo: str,
    run: dict[str, Any],
    job: dict[str, Any],
    *,
    attempt: int,
    pairing: str,
    pr: int | None = None,
    lookup: PrLookup | None = None,
    fix_sha: str,
    fix_run_id: object,
    fix_attempt: int | None,
    days: float,
    commits: int,
    files: list[Any],
) -> dict[str, Any]:
    additions = sum(int(f.get("additions", 0)) for f in files if isinstance(f, dict))
    deletions = sum(int(f.get("deletions", 0)) for f in files if isinstance(f, dict))
    return {
        "id": _candidate_id(repo, run.get("id"), attempt),
        "pairing": pairing,
        "pr": pr,
        "pr_lookup": lookup.value if lookup else None,
        "run_id": run.get("id"),
        "attempt": attempt,
        "workflow": clean_label(run.get("name")),
        "branch": clean_label(run.get("head_branch")),
        "event": clean_label(run.get("event"), 30),
        "job_id": job["job_id"],
        "job": clean_label(job["job"]),
        "step": clean_label(job["step"]),
        "failed_jobs": job["failed"],
        "failed_sha": run.get("head_sha"),
        "fix_sha": fix_sha,
        "fix_run_id": fix_run_id,
        "fix_attempt": fix_attempt,
        "days": round(days, 2),
        "commits": commits,
        "files": len(files),
        "additions": additions,
        "deletions": deletions,
    }


def render_candidates(results: Sequence[Discovery]) -> str:
    """``candidates.yaml``: data only, every third-party string already cleaned."""
    reasons: dict[str, int] = {}
    for result in results:
        for item in result.discarded:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    document = {
        "repos": [
            {
                "repo": r.repo,
                "license": r.license,
                "api_calls": r.api_calls,
                "stopped": r.stopped,
                "candidates": r.candidates,
                "discarded": r.discarded,
            }
            for r in results
        ],
        "discard_reasons": dict(sorted(reasons.items())),
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
