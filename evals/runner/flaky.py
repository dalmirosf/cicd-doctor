"""Simulated run-event sequences for the flaky and fix metrics (SPEC §11.2, D-107).

``resolved_flaky`` is not decided by a model: it comes out of the state machine of
``core/outcomes.py``. This module checks that machine on many sequences of run events, in many
arrival orders, against an **oracle that does not call it**: every scenario carries the label it was
built to have (``ORACLE``, written by hand). A generator that picked the label by running ``decide``
would measure nothing, the same rule as the injection suite.

It is a deterministic state machine, so 100 % is the expected value. What the metric buys is a
guard against regressions and the borders (forks, workflows, branchless runs, the same sha in
another run, duplicates, the order of arrival), not an estimate of a statistical accuracy.

``classify`` plays the worker: a failure is analysed once (first writer wins, so a duplicate
delivery changes nothing), is checked against the successes recorded before it (``decide_late``)
and, while it stays open, against each later success (``decide``).
"""

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from cicd_doctor.core.models import (
    FailedJob,
    Failure,
    FailureStatus,
    JobRef,
    LogExcerpt,
    LogLine,
    RunRef,
    Signature,
    SuccessRef,
    failure_id_for,
)
from cicd_doctor.core.outcomes import Outcome, decide, decide_late

DEFAULT_SEQUENCES = 300
DEFAULT_SEED = 20261120
_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

FLAKY = "resolved_flaky"
FIX = "resolved_by_commit"
OPEN = "open"
OUTCOMES = (FLAKY, FIX, OPEN)

# The label each scenario is built to have. Written by hand: the oracle never looks at the events.
ORACLE: dict[str, str] = {
    "flaky_rerun": FLAKY,  # the same run, a later attempt passes
    "flaky_rerun_late": FLAKY,  # ... and the success is recorded before the failure is analysed
    "fix_by_commit": FIX,  # a later run on another sha passes
    "fix_by_commit_late": FIX,  # ... recorded before the failure is analysed
    "fail_then_fail": OPEN,  # it fails again: the branch stays red
    "green_other_branch": OPEN,
    "green_other_workflow": OPEN,
    "green_fork_same_branch_name": OPEN,  # a fork's branch of the same name is another branch
    "green_without_branch": OPEN,  # tags and schedules have no branch to compare
    "same_sha_other_run": OPEN,  # another event on the same commit proves nothing
    "success_older_than_failure": OPEN,  # the failure is newer than that success
    "duplicate_delivery_flaky": FLAKY,
    "duplicate_delivery_fix": FIX,
}

Kind = Literal["failure", "success"]


@dataclass(frozen=True)
class RunEvent:
    kind: Kind
    run_id: int
    attempt: int
    sha: str
    branch: str | None = "feature/x"
    head_repo: str = "acme/widgets"
    workflow_id: int = 7
    repo_id: int = 100
    job_id: int = 1

    def ref(self) -> RunRef:
        return RunRef(
            installation_id=1,
            repo_id=self.repo_id,
            repo_full_name="acme/widgets",
            run_id=self.run_id,
            run_attempt=self.attempt,
            workflow_id=self.workflow_id,
            workflow_name="CI",
            head_sha=self.sha,
            head_branch=self.branch,
            head_repo_full_name=self.head_repo,
            event="pull_request",
            is_fork=self.head_repo != "acme/widgets",
            pr_number=None,
        )


@dataclass(frozen=True)
class EventSequence:
    """Events in arrival order. The failure to judge is the first ``failure`` event."""

    scenario: str
    events: tuple[RunEvent, ...]
    note: str = ""

    @property
    def expected(self) -> str:
        return ORACLE[self.scenario]


def failure_of(event: RunEvent) -> Failure:
    run = event.ref()
    job = JobRef(run=run, job_id=event.job_id, job_name="tests")
    return Failure(
        id=failure_id_for(event.run_id, event.attempt, event.job_id),
        job=FailedJob(
            job=job,
            conclusion="failure",
            step_name="Run tests",
            step_number=1,
            started_at=_T0,
            completed_at=_T0,
            runner_os="Linux",
        ),
        excerpt=LogExcerpt(
            job=job,
            step_name="Run tests",
            lines=[LogLine(number=1, text="boom")],
            omitted=[],
            error_lines=[1],
            token_count=1,
            truncated=False,
            redactions=0,
        ),
        signature=Signature(text="sig", hash=f"{event.job_id:064x}"),
        embedding=None,
        status=FailureStatus.OPEN,
        fix_commit_sha=None,
    )


def classify(sequence: EventSequence) -> str:
    """The status of the first failure of the sequence after ``decide`` and ``decide_late``."""
    target = next(e for e in sequence.events if e.kind == "failure")
    target_id = failure_of(target).id
    failures: dict[object, Failure] = {}
    recorded: list[RunEvent] = []
    for event in sequence.events:
        if event.kind == "failure":
            failure = failure_of(event)
            if failure.id in failures:
                continue  # a duplicate delivery: the checkpoint keeps the first
            ref = event.ref()
            same_branch = [
                SuccessRef(run_id=s.run_id, attempt=s.attempt, head_sha=s.sha)
                for s in recorded
                if _same_branch(s.ref(), ref)
            ]
            resolution = decide_late(failure, same_branch)
            if resolution is not None:
                failure = failure.model_copy(
                    update={
                        "status": resolution.status,
                        "fix_commit_sha": resolution.fix_commit_sha,
                    }
                )
            failures[failure.id] = failure
        else:
            recorded.append(event)
            open_failures = [f for f in failures.values() if f.status is FailureStatus.OPEN]
            for resolution in decide(Outcome(success=event.ref(), completed_at=_T0), open_failures):
                failures[resolution.failure_id] = failures[resolution.failure_id].model_copy(
                    update={
                        "status": resolution.status,
                        "fix_commit_sha": resolution.fix_commit_sha,
                    }
                )
    status = failures[target_id].status
    return {
        FailureStatus.RESOLVED_FLAKY: FLAKY,
        FailureStatus.RESOLVED_BY_COMMIT: FIX,
    }.get(status, OPEN)


def _same_branch(a: RunRef, b: RunRef) -> bool:
    """What the store query filters on before ``decide_late`` (repository, workflow, branch)."""
    return (
        a.repo_id == b.repo_id and a.workflow_id == b.workflow_id and a.branch_key == b.branch_key
    )


# --- the generator --------------------------------------------------------------------------


def _sha(rng: random.Random) -> str:
    return f"{rng.getrandbits(160):040x}"


def _base(rng: random.Random) -> dict[str, object]:
    return {
        "run_id": rng.randint(10_000, 900_000),
        "sha": _sha(rng),
        "branch": rng.choice(["feature/x", "fix/y", "main", "release/1.0"]),
        "workflow_id": rng.randint(1, 50),
    }


def _fail(b: dict[str, object], attempt: int = 1) -> RunEvent:
    return RunEvent(
        "failure",
        b["run_id"],  # type: ignore[arg-type]
        attempt,
        b["sha"],  # type: ignore[arg-type]
        b["branch"],  # type: ignore[arg-type]
        workflow_id=b["workflow_id"],  # type: ignore[arg-type]
    )


def _ok(b: dict[str, object], **changes: object) -> RunEvent:
    base = RunEvent(
        "success",
        b["run_id"],  # type: ignore[arg-type]
        2,
        b["sha"],  # type: ignore[arg-type]
        b["branch"],  # type: ignore[arg-type]
        workflow_id=b["workflow_id"],  # type: ignore[arg-type]
    )
    return RunEvent(**{**base.__dict__, **changes})


def _order(rng: random.Random, failure: RunEvent, success: RunEvent) -> tuple[RunEvent, ...]:
    """Failure first (the usual order) or the success first (a late analysis)."""
    return (failure, success) if rng.random() < 0.5 else (success, failure)


def _flaky_rerun(rng: random.Random, late: bool) -> EventSequence:
    b = _base(rng)
    fail, ok = _fail(b), _ok(b, attempt=rng.randint(2, 4))
    events = (ok, fail) if late else (fail, ok)
    return EventSequence("flaky_rerun_late" if late else "flaky_rerun", events)


def _fix(rng: random.Random, late: bool) -> EventSequence:
    b = _base(rng)
    fail = _fail(b, attempt=rng.randint(1, 3))
    ok = _ok(b, run_id=fail.run_id + rng.randint(1, 500), sha=_sha(rng), attempt=rng.randint(1, 2))
    events = (ok, fail) if late else (fail, ok)
    return EventSequence("fix_by_commit_late" if late else "fix_by_commit", events)


def _fail_then_fail(rng: random.Random) -> EventSequence:
    b = _base(rng)
    first = _fail(b)
    second = RunEvent(
        "failure",
        first.run_id + rng.randint(1, 500),
        1,
        _sha(rng),
        first.branch,
        workflow_id=first.workflow_id,
        job_id=2,
    )
    return EventSequence("fail_then_fail", (first, second))


def _green_elsewhere(rng: random.Random, scenario: str) -> EventSequence:
    b = _base(rng)
    fail = _fail(b)
    later = {"run_id": fail.run_id + rng.randint(1, 500), "sha": _sha(rng), "attempt": 1}
    if scenario == "green_other_branch":
        later["branch"] = "other/" + str(b["branch"])
    elif scenario == "green_other_workflow":
        later["workflow_id"] = fail.workflow_id + 1
    elif scenario == "green_fork_same_branch_name":
        later["head_repo"] = "stranger/widgets"
    ok = _ok(b, **later)
    return EventSequence(scenario, _order(rng, fail, ok))


def _green_without_branch(rng: random.Random) -> EventSequence:
    b = _base(rng)
    b["branch"] = None
    fail = _fail(b)
    ok = _ok(b, run_id=fail.run_id + rng.randint(1, 500), sha=_sha(rng), attempt=1)
    return EventSequence("green_without_branch", _order(rng, fail, ok))


def _same_sha_other_run(rng: random.Random) -> EventSequence:
    b = _base(rng)
    fail = _fail(b)
    ok = _ok(b, run_id=fail.run_id + rng.randint(1, 500), attempt=rng.randint(1, 2))
    return EventSequence("same_sha_other_run", _order(rng, fail, ok))


def _success_older(rng: random.Random) -> EventSequence:
    b = _base(rng)
    fail = _fail(b, attempt=rng.randint(1, 3))
    ok = _ok(b, run_id=fail.run_id - rng.randint(1, 500), sha=_sha(rng), attempt=1)
    return EventSequence("success_older_than_failure", _order(rng, fail, ok))


def _duplicate(rng: random.Random, fix: bool) -> EventSequence:
    inner = _fix(rng, late=False) if fix else _flaky_rerun(rng, late=False)
    fail, ok = inner.events
    events = (fail, fail, ok, ok) if rng.random() < 0.5 else (ok, ok, fail, fail)
    return EventSequence("duplicate_delivery_fix" if fix else "duplicate_delivery_flaky", events)


BUILDERS: dict[str, Callable[[random.Random], EventSequence]] = {
    "flaky_rerun": lambda r: _flaky_rerun(r, late=False),
    "flaky_rerun_late": lambda r: _flaky_rerun(r, late=True),
    "fix_by_commit": lambda r: _fix(r, late=False),
    "fix_by_commit_late": lambda r: _fix(r, late=True),
    "fail_then_fail": _fail_then_fail,
    "green_other_branch": lambda r: _green_elsewhere(r, "green_other_branch"),
    "green_other_workflow": lambda r: _green_elsewhere(r, "green_other_workflow"),
    "green_fork_same_branch_name": lambda r: _green_elsewhere(r, "green_fork_same_branch_name"),
    "green_without_branch": _green_without_branch,
    "same_sha_other_run": _same_sha_other_run,
    "success_older_than_failure": _success_older,
    "duplicate_delivery_flaky": lambda r: _duplicate(r, fix=False),
    "duplicate_delivery_fix": lambda r: _duplicate(r, fix=True),
}
assert set(BUILDERS) == set(ORACLE)


def generate(count: int = DEFAULT_SEQUENCES, seed: int = DEFAULT_SEED) -> list[EventSequence]:
    """``count`` sequences, the scenarios in turn so every one is covered, from a seeded RNG."""
    rng = random.Random(seed)
    names = sorted(BUILDERS)
    return [BUILDERS[names[i % len(names)]](rng) for i in range(count)]


# --- two sequences shaped like the ones the P2 live verification recorded ------------------------
# The ids are the ones of the evidence in SPEC §13 P2 (the lab: run 44381406019 re-run, run
# 44418654562 then a fix), but the sequences are REBUILT, not recorded: the fixtures of
# ``docs/phases/p2/fixtures`` are webhook payloads, not the event streams of those runs. They are
# synthetic equivalents and the report says so.

_LAB = "dalmirosff/cicd-doctor-lab"


def lab_sequences() -> list[EventSequence]:
    flaky = RunEvent("failure", 44381406019, 1, "1" * 40, "p2/flaky", head_repo=_LAB, workflow_id=9)
    fix = RunEvent("failure", 44418654562, 1, "2" * 40, "p2/fix", head_repo=_LAB, workflow_id=9)
    return [
        EventSequence(
            "flaky_rerun",
            (flaky, RunEvent(**{**flaky.__dict__, "kind": "success", "attempt": 2})),
            note="shaped like lab run 44381406019 (pr-flaky, attempt 2 passes); synthetic",
        ),
        EventSequence(
            "fix_by_commit",
            (
                fix,
                RunEvent(
                    "success", 44419250142, 1, "3" * 40, "p2/fix", head_repo=_LAB, workflow_id=9
                ),
            ),
            note="shaped like lab runs 44418654562 then 44419250142 (a new commit); synthetic",
        ),
    ]


# --- the metric -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakyMetrics:
    sequences: int
    correct: int
    accuracy: float | None
    flaky_precision: float | None  # of the sequences the machine called flaky, how many were
    flaky_recall: float | None  # of the ones built to be flaky, how many it called flaky
    by_commit_precision: float | None
    by_commit_recall: float | None
    by_scenario: dict[str, dict[str, int]]  # scenario -> {sequences, correct}
    misses: list[str] = field(default_factory=list)  # "scenario: expected X, got Y"
    lab_sequences: int = 0
    lab_correct: int = 0
    note: str = (
        "A deterministic state machine: 100% is expected. It guards against regressions and "
        "covers borders; it does not estimate a statistical accuracy (D-107). The 2 lab "
        "sequences are synthetic equivalents of the recorded P2 runs."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "sequences": self.sequences,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "resolved_flaky": {"precision": self.flaky_precision, "recall": self.flaky_recall},
            "resolved_by_commit": {
                "precision": self.by_commit_precision,
                "recall": self.by_commit_recall,
            },
            "by_scenario": self.by_scenario,
            "misses": self.misses,
            "lab_sequences": self.lab_sequences,
            "lab_correct": self.lab_correct,
            "note": self.note,
        }


def measure(sequences: Sequence[EventSequence], lab: Sequence[EventSequence] = ()) -> FlakyMetrics:
    """Classify every sequence against its oracle label; ``lab`` are the lab-shaped ones."""
    graded = [(s, s.expected, classify(s)) for s in [*sequences, *lab]]
    by_scenario: dict[str, dict[str, int]] = {}
    misses: list[str] = []
    for s, expected, got in graded:
        cell = by_scenario.setdefault(s.scenario, {"sequences": 0, "correct": 0})
        cell["sequences"] += 1
        cell["correct"] += expected == got
        if expected != got:
            misses.append(f"{s.scenario}: expected {expected}, got {got}")

    def ratio(part: int, whole: int) -> float | None:
        return round(part / whole, 4) if whole else None

    def prf(label: str) -> tuple[float | None, float | None]:
        hit = sum(e == label and g == label for _, e, g in graded)
        return (
            ratio(hit, sum(g == label for _, _, g in graded)),
            ratio(hit, sum(e == label for _, e, _ in graded)),
        )

    flaky_p, flaky_r = prf(FLAKY)
    fix_p, fix_r = prf(FIX)
    lab_graded = graded[len(sequences) :]
    correct = sum(e == g for _, e, g in graded)
    return FlakyMetrics(
        sequences=len(graded),
        correct=correct,
        accuracy=ratio(correct, len(graded)),
        flaky_precision=flaky_p,
        flaky_recall=flaky_r,
        by_commit_precision=fix_p,
        by_commit_recall=fix_r,
        by_scenario=by_scenario,
        misses=misses[:20],
        lab_sequences=len(lab_graded),
        lab_correct=sum(e == g for _, e, g in lab_graded),
    )


def run_flaky_metric(count: int = DEFAULT_SEQUENCES, seed: int = DEFAULT_SEED) -> FlakyMetrics:
    return measure(generate(count, seed), lab_sequences())
