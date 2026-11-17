"""The whole diagnosis flow with the REAL GitHub adapters over ``FakeGitHub`` (P1c d2).

Used by the offline end-to-end tests (in-memory queue, store and ledger) and by the Postgres tests
(the Postgres adapters): the same wiring as ``app/container.py``, with the GitHub side served by
``tests/github_fake.py`` and the LLM by ``FakeLLM``. Nothing here reaches a network.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric import rsa

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import GitHubAppAuth
from cicd_doctor.adapters.github.http import GitHubHttp
from cicd_doctor.adapters.github.logs import GitHubLogSource
from cicd_doctor.adapters.github.pulls import GitHubPullRequests
from cicd_doctor.adapters.github.reporter import GitHubReporter
from cicd_doctor.adapters.github.webhooks import route_event
from cicd_doctor.adapters.memory import CharTokenCounter, FakeLLM
from cicd_doctor.adapters.memory.llm import ScriptItem, valid
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.core.ports import Embedder, FailureStore, InstallationStore, JobQueue, UsageLedger
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig, DiagnoseFailedRun
from cicd_doctor.core.usecases.record_run_outcome import RecordRunOutcome
from cicd_doctor.worker import Worker
from cicd_doctor.worker.handlers import diagnose_run_handler, record_outcome_handler
from tests.github_fake import (
    FakeGitHub,
    FakeRepoWrites,
    GitHubLogsFake,
    RecordedRequest,
    jobs_json,
)
from tests.webhooks import load

# The ids of ``tests/fixtures/webhooks/workflow_run_completed_failure.json``.
INSTALLATION_ID = 10
REPO_ID = 100
REPO = "acme/widgets"
PR_NUMBER = 12
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))

# The failed step of ``jobs_json`` runs from 12:01 to 12:04 on this day.
LOG_START = datetime.fromisoformat("2026-11-12T12:02:00+00:00")
LOG_BODY = [
    "Run pytest",
    "Traceback (most recent call last):",
    '  File "src/app/cli.py", line 3, in <module>',
    "    import requests",
    "ModuleNotFoundError: No module named 'requests'",
    "##[error]Process completed with exit code 1.",
]


@cache
def app_key() -> rsa.RSAPrivateKey:
    """The key of the fake app: generated once per test process, never written to disk."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def log_bytes(body: Sequence[str] = LOG_BODY) -> bytes:
    lines = [
        f"{(LOG_START + timedelta(milliseconds=100 * i)).strftime('%Y-%m-%dT%H:%M:%S.%f')}0Z {line}"
        for i, line in enumerate(body)
    ]
    return "\n".join(lines).encode()


def job_id_for(run_id: int, attempt: int) -> int:
    return run_id * 10 + attempt


def workflow_run_payload(
    run_id: int = 5000,
    attempt: int = 1,
    conclusion: str = "failure",
    head_sha: str | None = None,
) -> dict[str, Any]:
    """The signed-webhook fixture of a failed run, as another run (attempt) of the same PR."""
    payload = load("workflow_run_completed_failure")
    payload["workflow_run"]["id"] = run_id
    payload["workflow_run"]["run_attempt"] = attempt
    payload["workflow_run"]["conclusion"] = conclusion
    if head_sha is not None:
        payload["workflow_run"]["head_sha"] = head_sha
    return payload


class InspectableQueue(JobQueue, Protocol):
    """The queues of the adapters, which can show a job by id (the port does not)."""

    async def get(self, job_id: int) -> Job | None: ...


@dataclass
class DiagnoseWorld:
    clock: FakeClock
    github: FakeGitHub
    writes: FakeRepoWrites
    logs: GitHubLogsFake
    queue: InspectableQueue
    store: FailureStore
    ledger: UsageLedger
    installations: InstallationStore
    llm: FakeLLM
    use_case: DiagnoseFailedRun
    worker: Worker

    def add_run(
        self,
        run_id: int = 5000,
        attempt: int = 1,
        body: bytes | None = None,
        job_name: str | None = None,
    ) -> int:
        """Make GitHub know the attempt: one failed job and its log. Returns the job id."""
        job_id = job_id_for(run_id, attempt)
        self.logs.set_jobs(run_id, attempt, jobs_json([job_id], run_attempt=attempt, name=job_name))
        self.logs.set_log(job_id, body if body is not None else log_bytes())
        return job_id

    def add_cancelled_run(
        self, run_id: int = 5000, attempt: int = 1, annotations: list[str] | None = None
    ) -> int:
        """A cancelled attempt: one cancelled job with its annotations (D-73) and its log, which
        only says the operation was cancelled. Returns the job id."""
        job_id = job_id_for(run_id, attempt)
        self.logs.set_jobs(
            run_id, attempt, jobs_json([job_id], run_attempt=attempt, conclusion="cancelled")
        )
        self.logs.set_annotations(
            job_id,
            [
                {"annotation_level": "failure", "message": message, "title": "", "path": ".github"}
                for message in annotations or []
            ],
        )
        self.logs.set_log(
            job_id, log_bytes(["Run sleep 300", "##[error]The operation was canceled."])
        )
        return job_id

    async def enqueue_success(
        self, run_id: int = 5001, attempt: int = 1, head_sha: str = "b" * 40
    ) -> None:
        """The ``record_outcome`` job the webhook would create for a successful attempt."""
        new_job = route_event(
            "workflow_run",
            workflow_run_payload(run_id, attempt, "success", head_sha),
            received_at=self.clock.now(),
            max_attempts=5,
        )
        assert new_job is not None
        assert await self.queue.enqueue(new_job, None)

    async def enqueue(self, run_id: int = 5000, attempt: int = 1) -> None:
        """The job the webhook would create for this attempt (without the HTTP hop)."""
        new_job = route_event(
            "workflow_run",
            workflow_run_payload(run_id, attempt),
            received_at=self.clock.now(),
            max_attempts=5,
        )
        assert new_job is not None
        assert await self.queue.enqueue(new_job, None)

    def calls(self) -> list[tuple[str, str | None]]:
        """Every request FakeGitHub saw, as ``(method, route template)``."""
        return [(r.method, r.template) for r in self.github.requests]

    def requests(self, method: str, template: str) -> list[RecordedRequest]:
        return self.github.requests_to(method, template)


@asynccontextmanager
async def diagnose_world(
    *,
    clock: FakeClock,
    queue: InspectableQueue,
    store: FailureStore,
    ledger: UsageLedger,
    installations: InstallationStore,
    llm_script: Sequence[ScriptItem] | None = None,
    embedder: Embedder | None = None,
    give_up_timeout_s: float = 5.0,
    lease: timedelta = timedelta(minutes=5),
) -> AsyncIterator[DiagnoseWorld]:
    github = FakeGitHub(clock, app_key().public_key())
    github.install(INSTALLATION_ID, repo_ids={REPO_ID})
    writes = FakeRepoWrites(github)
    logs = GitHubLogsFake(github)
    llm = FakeLLM([valid()] * 8 if llm_script is None else llm_script)

    async def no_sleep(delay: float) -> None:
        return None

    async with GitHubHttp(clock, transport=github.transport()) as http:
        auth = GitHubAppAuth(http, app_id=github.app_id, private_key=app_key(), clock=clock)
        log_source = GitHubLogSource(http, auth, blob_transport=github.transport())
        try:
            use_case = DiagnoseFailedRun(
                logs=log_source,
                pulls=GitHubPullRequests(http, auth),
                reporter=GitHubReporter(http, auth, clock),
                store=store,
                installations=installations,
                counter=CharTokenCounter(),
                diagnose=DiagnoseJob(
                    llm=llm, ledger=ledger, cost=PRICES, max_output_tokens=800, sleep=no_sleep
                ),
                clock=clock,
                embedder=embedder,
                config=DiagnoseConfig(),
            )
            handler = diagnose_run_handler(use_case, give_up_timeout_s=give_up_timeout_s)
            worker = Worker(
                queue,
                {
                    JobKind.DIAGNOSE_RUN: handler,
                    JobKind.RECORD_OUTCOME: record_outcome_handler(
                        RecordRunOutcome(store=store, installations=installations)
                    ),
                },
                clock,
                concurrency=1,
                lease=lease,
                jitter=lambda: 0.0,
            )
            yield DiagnoseWorld(
                clock=clock,
                github=github,
                writes=writes,
                logs=logs,
                queue=queue,
                store=store,
                ledger=ledger,
                installations=installations,
                llm=llm,
                use_case=use_case,
                worker=worker,
            )
        finally:
            await log_source.aclose()


def body_of(request: RecordedRequest) -> dict[str, Any]:
    assert isinstance(request.json, dict)
    return request.json
