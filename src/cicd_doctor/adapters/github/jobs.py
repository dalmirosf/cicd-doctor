"""Normalise the GitHub jobs API response into ``WorkflowJob`` models (D-58).

``GET /repos/{o}/{r}/actions/runs/{run_id}/attempts/{n}/jobs`` returns one page of
``{"total_count": N, "jobs": [...]}``. This module only parses; the HTTP client that pages
through the endpoint arrives in P1c. The eval cases store the same JSON (``jobs.json``).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from cicd_doctor.core.models import WorkflowJob, WorkflowStep

# Label prefixes of GitHub-hosted runners. Self-hosted runners carry "self-hosted" plus an
# OS label ("Linux", "Windows", "macOS"), which the same lookup handles.
_RUNNER_OS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ubuntu", "Linux"),
    ("linux", "Linux"),
    ("windows", "Windows"),
    ("macos", "macOS"),
)


class InvalidJobsPayloadError(ValueError):
    """The jobs payload lacks a field we need. The message names fields, never values."""


class _Step(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    name: str
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class _Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    labels: list[str] = []
    steps: list[_Step] | None = None


def runner_os(labels: Sequence[str]) -> str | None:
    """The runner OS from the job's labels, or None when no label names one."""
    for label in labels:
        lowered = label.lower()
        for prefix, os_name in _RUNNER_OS_PREFIXES:
            if lowered.startswith(prefix):
                return os_name
    return None


def parse_jobs(payload: Mapping[str, Any]) -> list[WorkflowJob]:
    """Turn one page of the jobs API response into ``WorkflowJob`` models, in API order."""
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise InvalidJobsPayloadError("jobs payload has no 'jobs' list")
    jobs: list[WorkflowJob] = []
    for index, raw in enumerate(raw_jobs):
        try:
            job = _Job.model_validate(raw)
        except ValidationError as exc:
            fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
            raise InvalidJobsPayloadError(
                f"job #{index} is missing or has invalid fields: {', '.join(fields)}"
            ) from None
        jobs.append(
            WorkflowJob(
                job_id=job.id,
                name=job.name,
                conclusion=job.conclusion,
                started_at=job.started_at,
                completed_at=job.completed_at,
                runner_os=runner_os(job.labels),
                steps=[
                    WorkflowStep(
                        number=step.number,
                        name=step.name,
                        conclusion=step.conclusion,
                        started_at=step.started_at,
                        completed_at=step.completed_at,
                    )
                    for step in job.steps or []
                ],
            )
        )
    return jobs
