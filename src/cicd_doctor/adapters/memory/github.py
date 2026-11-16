"""Programmable fakes of the GitHub ports, for the use case tests (P1c).

``FakeLogSource``, ``FakeReporter`` and ``FakePullRequests`` are recorders: each keeps the exact
sequence of its calls, answers what the test programmed and raises the failures the test asked
for. The reporter also keeps state (check runs and comments), with the same find-or-create rules
as ``GitHubReporter`` (D-70), so a test can count what was really published.

They live in ``adapters/memory`` and not in ``core``: a use case never imports an adapter.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cicd_doctor.core.models import (
    CheckOutput,
    CommentRef,
    CommentTarget,
    JobRef,
    RawLog,
    RunRef,
    Suggestion,
    WorkflowJob,
)
from cicd_doctor.core.render import check_name


class FakeLogSource:
    """Implements ``LogSource``: the jobs of each run attempt and the log of each job."""

    def __init__(self, journal: list[str] | None = None) -> None:
        self._jobs: dict[tuple[int, int], list[WorkflowJob]] = {}
        self._logs: dict[int, RawLog] = {}
        self._jobs_error: Exception | None = None
        self._log_errors: dict[int, Exception] = {}
        self._annotations: dict[int, list[str]] = {}
        self._annotation_errors: dict[int, Exception] = {}
        # ("jobs", run_id, attempt), ("job_log", job, 0), ("job_annotations", job, 0)
        self.calls: list[tuple[str, int, int]] = []
        self.max_bytes: list[int] = []
        self._journal = journal

    def add_jobs(self, run: RunRef, jobs: Sequence[WorkflowJob]) -> None:
        self._jobs[(run.run_id, run.run_attempt)] = list(jobs)

    def add_log(self, job_id: int, text: str, *, truncated: bool = False) -> None:
        self._logs[job_id] = RawLog(text=text, truncated=truncated)

    def add_annotations(self, job_id: int, messages: Sequence[str]) -> None:
        self._annotations[job_id] = list(messages)

    def fail_annotations(self, job_id: int, error: Exception | None) -> None:
        """Make ``job_annotations`` of ``job_id`` raise ``error`` (``None``: stop failing)."""
        if error is None:
            self._annotation_errors.pop(job_id, None)
        else:
            self._annotation_errors[job_id] = error

    def fail_jobs(self, error: Exception | None) -> None:
        """Make ``jobs`` raise ``error`` (``None``: stop failing)."""
        self._jobs_error = error

    def fail_log(self, job_id: int, error: Exception | None) -> None:
        """Make ``job_log`` of ``job_id`` raise ``error`` (``None``: stop failing)."""
        if error is None:
            self._log_errors.pop(job_id, None)
        else:
            self._log_errors[job_id] = error

    @property
    def downloads(self) -> int:
        """How many job logs were requested."""
        return sum(1 for name, _, _ in self.calls if name == "job_log")

    async def jobs(self, run: RunRef) -> list[WorkflowJob]:
        self.calls.append(("jobs", run.run_id, run.run_attempt))
        _note(self._journal, "log_source.jobs")
        await asyncio.sleep(0)
        if self._jobs_error is not None:
            raise self._jobs_error
        return list(self._jobs.get((run.run_id, run.run_attempt), []))

    async def job_annotations(self, job: JobRef) -> list[str]:
        self.calls.append(("job_annotations", job.job_id, 0))
        _note(self._journal, "log_source.job_annotations")
        await asyncio.sleep(0)
        error = self._annotation_errors.get(job.job_id)
        if error is not None:
            raise error
        return list(self._annotations.get(job.job_id, []))

    async def job_log(self, job: JobRef, max_bytes: int) -> RawLog:
        self.calls.append(("job_log", job.job_id, 0))
        self.max_bytes.append(max_bytes)
        _note(self._journal, "log_source.job_log")
        await asyncio.sleep(0)
        error = self._log_errors.get(job.job_id)
        if error is not None:
            raise error
        return self._logs[job.job_id]  # KeyError: the test forgot to add the log


@dataclass
class FakeCheck:
    """A check run as the fake reporter keeps it."""

    id: int
    external_id: str
    name: str
    status: str = "in_progress"
    output: CheckOutput | None = None
    updates: int = 0  # how many times it was written


@dataclass
class FakeComment:
    id: int
    body: str
    edits: int = 0


@dataclass(frozen=True)
class ReporterCall:
    """One call to the reporter: which method, for which run and with which id hint."""

    method: str
    run_id: int
    attempt: int
    known_id: int | None = None
    check_run_id: int | None = None


@dataclass
class _Failures:
    pending: dict[str, list[Exception]] = field(default_factory=dict)

    def take(self, method: str) -> Exception | None:
        queue = self.pending.get(method)
        return queue.pop(0) if queue else None


class FakeReporter:
    """Implements ``Reporter`` over in-memory check runs and comments.

    It follows the find-or-create rules of ``GitHubReporter``: ``start_check`` takes the known id,
    else finds the check run by its run, else creates it; ``upsert_comment`` takes the known id,
    else finds the comment with the marker of ``body``, else creates it. Every method yields to
    the event loop once, so two writers that are not serialised by a lock overlap and, for
    example, both create a comment.
    """

    def __init__(
        self,
        *,
        first_check_id: int = 9001,
        first_comment_id: int = 7001,
        journal: list[str] | None = None,
    ) -> None:
        self.checks: dict[int, FakeCheck] = {}
        self.comments: dict[int, FakeComment] = {}
        self.calls: list[ReporterCall] = []
        self.comment_bodies: list[str] = []  # every body ever sent, in order
        self._next_check = first_check_id
        self._next_comment = first_comment_id
        self._failures = _Failures()
        self._journal = journal

    def fail_next(self, method: str, error: Exception, times: int = 1) -> None:
        """Make the next ``times`` calls of ``method`` raise ``error`` (after recording them)."""
        self._failures.pending.setdefault(method, []).extend([error] * times)

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    async def start_check(self, run: RunRef, known_id: int | None) -> int:
        self.calls.append(ReporterCall("start_check", run.run_id, run.run_attempt, known_id))
        await self._enter("start_check")
        external_id = f"cicd-doctor:run:{run.run_id}"
        check = self.checks.get(known_id) if known_id is not None else None
        if check is None:
            check = next((c for c in self.checks.values() if c.external_id == external_id), None)
        if check is None:
            check = FakeCheck(self._next_check, external_id, check_name(run.workflow_name))
            self._next_check += 1
            self.checks[check.id] = check
        check.status = "in_progress"
        check.name = check_name(run.workflow_name)
        check.updates += 1
        return check.id

    async def complete_check(self, run: RunRef, check_run_id: int, output: CheckOutput) -> None:
        self.calls.append(
            ReporterCall("complete_check", run.run_id, run.run_attempt, check_run_id=check_run_id)
        )
        await self._enter("complete_check")
        check = self.checks[check_run_id]  # KeyError: completing a check that was never started
        check.status = "completed"
        check.output = output
        check.updates += 1

    async def upsert_comment(self, target: CommentTarget, body: str, known_id: int | None) -> int:
        run = target.run
        self.calls.append(ReporterCall("upsert_comment", run.run_id, run.run_attempt, known_id))
        self.comment_bodies.append(body)
        await self._enter("upsert_comment")
        marker = body.splitlines()[0] if body else ""
        comment = self.comments.get(known_id) if known_id is not None else None
        if comment is None:
            comment = next(
                (c for c in self.comments.values() if c.body.splitlines()[:1] == [marker]), None
            )
        if comment is None:
            comment = FakeComment(self._next_comment, body)
            self._next_comment += 1
            self.comments[comment.id] = comment
        else:
            comment.body = body
            comment.edits += 1
        return comment.id

    async def post_suggestion(self, target: CommentTarget, suggestion: Suggestion) -> None:
        raise NotImplementedError("P3")

    async def react(self, comment: CommentRef, reaction: str) -> None:
        raise NotImplementedError("P3")

    async def _enter(self, method: str) -> None:
        _note(self._journal, f"reporter.{method}")
        await asyncio.sleep(0)
        error = self._failures.take(method)
        if error is not None:
            raise error


class FakePullRequests:
    """Implements ``PullRequestSource``: the pull request number programmed for each run id."""

    def __init__(
        self, numbers: Mapping[int, int | None] | None = None, journal: list[str] | None = None
    ) -> None:
        self._numbers = dict(numbers or {})
        self._error: Exception | None = None
        self.calls: list[RunRef] = []
        self._journal = journal

    def set(self, run_id: int, number: int | None) -> None:
        self._numbers[run_id] = number

    def fail(self, error: Exception | None) -> None:
        self._error = error

    async def find(self, run: RunRef) -> int | None:
        self.calls.append(run)
        _note(self._journal, "pulls.find")
        await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        return self._numbers.get(run.run_id)


def _note(journal: list[str] | None, event: str) -> None:
    if journal is not None:
        journal.append(event)
