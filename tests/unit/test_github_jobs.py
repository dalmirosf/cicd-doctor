"""Parsing the GitHub jobs API response (D-58)."""

import json
from datetime import UTC, datetime

import pytest

from cicd_doctor.adapters.github.jobs import InvalidJobsPayloadError, parse_jobs, runner_os
from tests.logcases import LOG_CASES_DIR


def test_parses_jobs_and_steps_of_a_fixture() -> None:
    payload = json.loads((LOG_CASES_DIR / "pytest-assert" / "jobs.json").read_text("utf-8"))

    [job] = parse_jobs(payload)

    assert job.job_id == 71000000001
    assert job.name == "test"
    assert job.conclusion == "failure"
    assert job.runner_os == "Linux"
    assert job.completed_at is not None
    assert [step.number for step in job.steps] == list(range(1, 8))
    failed = job.steps[4]
    assert failed.name == "Run uv run pytest -v"
    assert failed.conclusion == "failure"
    assert failed.started_at == datetime(2026, 11, 13, 10, 0, 9, tzinfo=UTC)


def test_missing_optional_fields_become_none() -> None:
    [job] = parse_jobs({"jobs": [{"id": 1, "name": "x"}]})

    assert job.conclusion is None
    assert job.started_at is None
    assert job.runner_os is None
    assert job.steps == []


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["ubuntu-latest"], "Linux"),
        (["ubuntu-24.04-arm"], "Linux"),
        (["self-hosted", "Linux", "X64"], "Linux"),
        (["windows-2025"], "Windows"),
        (["macos-15"], "macOS"),
        (["my-runner-group"], None),
        ([], None),
    ],
)
def test_runner_os_comes_from_the_labels(labels: list[str], expected: str | None) -> None:
    assert runner_os(labels) == expected


def test_a_payload_without_jobs_is_rejected() -> None:
    with pytest.raises(InvalidJobsPayloadError, match="no 'jobs' list"):
        parse_jobs({"total_count": 0})


def test_an_invalid_job_names_the_fields_but_not_the_values() -> None:
    payload = {"jobs": [{"id": 1, "name": "ok"}, {"id": "not-a-number-SECRETVALUE"}]}

    with pytest.raises(InvalidJobsPayloadError) as raised:
        parse_jobs(payload)

    message = str(raised.value)
    assert "job #1" in message
    assert "id" in message
    assert "name" in message
    assert "SECRETVALUE" not in message
