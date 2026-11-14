"""Failed job and failed step selection (FR-06, SPEC §7.1 step 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from cicd_doctor.core.logs import failed_step, select_failed_jobs
from cicd_doctor.core.models import WorkflowJob, WorkflowStep
from tests.factories import make_run_ref
from tests.logcases import load_log_case

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def _step(number: int, conclusion: str | None, name: str | None = None) -> WorkflowStep:
    return WorkflowStep(
        number=number,
        name=name or f"step {number}",
        conclusion=conclusion,
        started_at=T0 + timedelta(seconds=number),
        completed_at=T0 + timedelta(seconds=number + 1),
    )


def _job(
    job_id: int, conclusion: str | None, done: int | None, steps: list[WorkflowStep]
) -> WorkflowJob:
    return WorkflowJob(
        job_id=job_id,
        name=f"job {job_id}",
        conclusion=conclusion,
        started_at=T0,
        completed_at=T0 + timedelta(seconds=done) if done is not None else None,
        runner_os="Linux",
        steps=steps,
    )


def test_multi_job_fixture_selects_three_failed_jobs_earliest_first() -> None:
    case = load_log_case("multi-job")

    selected = select_failed_jobs(make_run_ref(), case.jobs, limit=3)

    assert [job.job.job_id for job in selected] == case.selected_job_ids


def test_success_skipped_cancelled_and_unfinished_jobs_are_not_selected() -> None:
    jobs = [
        _job(1, "success", 1, []),
        _job(2, "skipped", 2, []),
        _job(3, "cancelled", 3, []),
        _job(4, None, None, []),
        _job(5, "failure", None, []),  # no completion time: still running
        _job(6, "timed_out", 9, []),
        _job(7, "failure", 8, []),
    ]

    selected = select_failed_jobs(make_run_ref(), jobs, limit=3)

    assert [(job.job.job_id, job.conclusion) for job in selected] == [
        (7, "failure"),
        (6, "timed_out"),
    ]


def test_ties_on_completion_time_are_broken_by_job_id() -> None:
    jobs = [_job(9, "failure", 5, []), _job(3, "failure", 5, []), _job(4, "failure", 5, [])]

    assert [job.job.job_id for job in select_failed_jobs(make_run_ref(), jobs, limit=2)] == [3, 4]


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limit"):
        select_failed_jobs(make_run_ref(), [], limit=0)


def test_failed_step_is_the_first_failure() -> None:
    job = _job(1, "failure", 9, [_step(3, "failure"), _step(1, "success"), _step(2, "failure")])

    step = failed_step(job)

    assert step is not None
    assert step.number == 2


def test_failed_step_of_a_timed_out_job_is_the_cancelled_one() -> None:
    steps = [_step(1, "success"), _step(2, "cancelled"), _step(3, "cancelled")]

    step = failed_step(_job(1, "timed_out", 9, steps))

    assert step is not None
    assert step.number == 2


def test_no_failed_step_means_the_whole_job() -> None:
    selected = select_failed_jobs(make_run_ref(), [_job(1, "failure", 9, [_step(1, "success")])], 3)

    assert failed_step(_job(1, "failure", 9, [])) is None
    assert selected[0].step_name is None
    assert selected[0].step_started_at is None


def test_failed_job_carries_the_run_the_step_and_its_times() -> None:
    run = make_run_ref()
    job = _job(42, "failure", 30, [_step(1, "success"), _step(2, "failure", "Run pytest")])
    job = job.model_copy(update={"started_at": None})

    failed = select_failed_jobs(run, [job], limit=3)[0]

    assert failed.job.run == run
    assert failed.job.job_id == 42
    assert failed.job.job_name == "job 42"
    assert failed.conclusion == "failure"
    assert (failed.step_name, failed.step_number) == ("Run pytest", 2)
    assert failed.step_started_at == T0 + timedelta(seconds=2)
    assert failed.step_completed_at == T0 + timedelta(seconds=3)
    assert failed.started_at == failed.completed_at == T0 + timedelta(seconds=30)  # no start time
    assert failed.runner_os == "Linux"
