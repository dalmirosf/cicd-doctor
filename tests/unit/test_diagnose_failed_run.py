"""The ``diagnose_failed_run`` use case on in-memory fakes (P1c d1, D-70, D-71, D-75).

The central guarantee of D-70: the diagnosis of an attempt is computed once and published from
what is stored, so a retry after a failure while publishing makes no LLM call, no log download and
no budget reservation, and reuses the ids of the check run and of the comment.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.http import GitHubPermanentError
from cicd_doctor.adapters.memory import (
    CharTokenCounter,
    FakeLLM,
    FakeLogSource,
    FakePullRequests,
    FakeReporter,
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryUsageLedger,
)
from cicd_doctor.adapters.memory.llm import Fail, ScriptItem, valid
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from cicd_doctor.core.errors import LLMError, LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.models import (
    DiagnoseRunPayload,
    DiagnosisRequest,
    Installation,
    LLMResult,
    Publication,
    PublicationKey,
    PublicationKind,
    RunAnalysisRecord,
    RunRef,
    WorkflowJob,
    WorkflowStep,
)
from cicd_doctor.core.render import COULD_NOT_DIAGNOSE, FALLBACK_TEXT
from cicd_doctor.core.usecases.diagnose_failed_run import DiagnoseConfig, DiagnoseFailedRun
from tests.factories import T0, make_run_ref
from tests.redaction_corpus import planted_secrets

PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
COMPLETED_AT = T0 + timedelta(minutes=2)
MARKER = "<!-- cicd-doctor:v1 pr=12 workflow=7 -->"

LOG_BODY = [
    "Run pytest",
    "Traceback (most recent call last):",
    '  File "src/app/cli.py", line 3, in <module>',
    "    import requests",
    "ModuleNotFoundError: No module named 'requests'",
    "##[error]Process completed with exit code 1.",
]


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def log_text(extra: Sequence[str] = ()) -> str:
    """A GitHub-format log: the failed step runs between 30 s and 90 s after the job started."""
    lines = [*extra, *LOG_BODY]
    return "\n".join(
        f"{stamp(T0 + timedelta(seconds=31) + timedelta(milliseconds=100 * i))} {line}"
        for i, line in enumerate(lines)
    )


def workflow_job(job_id: int, conclusion: str = "failure", name: str = "tests") -> WorkflowJob:
    return WorkflowJob(
        job_id=job_id,
        name=name,
        conclusion=conclusion,
        started_at=T0,
        completed_at=COMPLETED_AT,
        runner_os="Linux",
        steps=[
            WorkflowStep(
                number=1,
                name="Set up job",
                conclusion="success",
                started_at=T0,
                completed_at=T0 + timedelta(seconds=30),
            ),
            WorkflowStep(
                number=2,
                name="Run pytest",
                conclusion=conclusion,
                started_at=T0 + timedelta(seconds=30),
                completed_at=T0 + timedelta(seconds=90),
            ),
        ],
    )


def payload_for(run: RunRef | None = None, **changes: Any) -> DiagnoseRunPayload:
    fields: dict[str, Any] = {
        "run": run or make_run_ref(),
        "conclusion": "failure",
        "completed_at": COMPLETED_AT,
        "default_branch": "main",
        "private": True,
        "account_login": "acme",
        "account_type": "Organization",
    }
    return DiagnoseRunPayload(**(fields | changes))


class FlakyStore(InMemoryFailureStore):
    """A store whose next comment ``save_publication`` calls fail, as a crash would."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.fail_comment_saves = 0

    async def save_publication(self, pub: Publication) -> bool:
        if self.fail_comment_saves and pub.key.kind is PublicationKind.COMMENT:
            self.fail_comment_saves -= 1
            raise TransientError("the database went away")
        return await super().save_publication(pub)


class CountingLLM:
    """Answers like ``FakeLLM`` and remembers how many calls were in flight at once."""

    def __init__(self, answers: int) -> None:
        self.inner = FakeLLM([valid()] * answers)
        self.active = 0
        self.peak = 0

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return await self.inner.diagnose(request)
        finally:
            self.active -= 1


class World:
    """The use case with every port faked, one failed job (id 11) of attempt 1 by default."""

    def __init__(
        self,
        script: Sequence[ScriptItem] | None = None,
        *,
        run: RunRef | None = None,
        with_llm: bool = True,
        config: DiagnoseConfig | None = None,
        daily_budget: Decimal = Decimal("2.00"),
        jobs: bool = True,
        llm: Any = None,
    ) -> None:
        self.clock = FakeClock(COMPLETED_AT + timedelta(seconds=5))
        self.journal: list[str] = []
        self.run = run or make_run_ref()
        self.logs = FakeLogSource(self.journal)
        self.reporter = FakeReporter(journal=self.journal)
        self.pulls = FakePullRequests(journal=self.journal)
        self.store = FlakyStore(self.clock)
        self.installations = InMemoryInstallationStore()
        self.ledger = InMemoryUsageLedger(self.clock, daily_budget_usd=daily_budget)
        self.llm = FakeLLM([valid()] if script is None else script)
        self.diagnose = DiagnoseJob(
            llm=(llm or self.llm) if with_llm else None,
            ledger=self.ledger,
            cost=PRICES,
            max_output_tokens=800,
            sleep=_no_sleep,
        )
        self.usecase = DiagnoseFailedRun(
            logs=self.logs,
            pulls=self.pulls,
            reporter=self.reporter,
            store=self.store,
            installations=self.installations,
            counter=CharTokenCounter(),
            diagnose=self.diagnose,
            clock=self.clock,
            config=config,
        )
        if jobs:
            self.add_failed_job(self.run, 11)

    def add_failed_job(self, run: RunRef, job_id: int, extra: Sequence[str] = ()) -> None:
        self.logs.add_jobs(run, [workflow_job(job_id), workflow_job(job_id + 1000, "success")])
        self.logs.add_log(job_id, log_text(extra))

    async def run_once(self, run: RunRef | None = None, **changes: Any) -> None:
        await self.usecase.run(payload_for(run or self.run, **changes))

    def calls_made(self) -> tuple[int, int, int]:
        """LLM calls, log downloads and budget reservations so far."""
        reserved = self.ledger.usage(self.clock.now().date(), self.run.installation_id).calls
        return len(self.llm.calls), self.logs.downloads, reserved

    async def record(self, run: RunRef | None = None) -> RunAnalysisRecord:
        run = run or self.run
        stored = await self.store.run_analysis(run.run_id, run.run_attempt)
        assert stored is not None
        return stored

    def check_key(self, run: RunRef | None = None) -> PublicationKey:
        run = run or self.run
        return PublicationKey.check(run.repo_id, run.run_id)

    def comment_key(self, pr_number: int = 12) -> PublicationKey:
        return PublicationKey.comment(self.run.repo_id, pr_number, self.run.workflow_id)


async def _no_sleep(delay: float) -> None:
    return None


# --- the happy path --------------------------------------------------------------------------


async def test_a_failed_run_gets_one_completed_check_and_one_comment() -> None:
    world = World()

    await world.run_once()

    [check] = world.reporter.checks.values()
    assert check.status == "completed"
    assert check.name == "CICD Doctor: CI"
    assert check.output is not None
    assert check.output.title.startswith("Likely cause: dependency")
    [comment] = world.reporter.comments.values()
    assert comment.body.splitlines()[0] == MARKER
    check_pub = await world.store.publication(world.check_key())
    comment_pub = await world.store.publication(world.comment_key())
    assert check_pub is not None
    assert check_pub.external_id == check.id
    assert comment_pub is not None
    assert comment_pub.external_id == comment.id
    assert (check_pub.run_id, check_pub.attempt) == (5000, 1)
    assert world.logs.max_bytes == [20_971_520]
    assert world.calls_made() == (1, 1, 1)


async def test_the_check_starts_before_anything_else_is_read_from_github() -> None:
    world = World()

    await world.run_once()

    assert world.journal == [
        "reporter.start_check",
        "log_source.jobs",
        "log_source.job_log",
        "reporter.complete_check",
        "reporter.upsert_comment",
    ]


async def test_the_analysis_is_stored_as_the_checkpoint() -> None:
    world = World()

    await world.run_once()

    record = await world.record()
    [item] = record.items
    assert item.failure.job.job.job_id == 11
    assert item.published.source == "llm"
    assert item.usage is not None
    assert item.usage.calls == 1
    assert record.attempt.conclusion == "failure"
    assert await world.installations.repository(100) is not None


async def test_without_a_pull_request_there_is_no_comment() -> None:
    world = World(run=make_run_ref(pr_number=None, event="push"))

    await world.run_once()

    assert len(world.reporter.checks) == 1
    assert world.reporter.comments == {}
    assert len(world.pulls.calls) == 1  # it was looked up and nothing was found
    assert await world.store.publication(world.comment_key()) is None


async def test_a_pull_request_found_by_the_source_gets_the_comment_and_is_stored() -> None:
    world = World(run=make_run_ref(pr_number=None))
    world.pulls.set(5000, 33)

    await world.run_once()

    record = await world.record()
    assert record.run.pr_number == 33
    [comment] = world.reporter.comments.values()
    assert comment.body.splitlines()[0] == "<!-- cicd-doctor:v1 pr=33 workflow=7 -->"
    assert await world.store.publication(world.comment_key(33)) is not None


async def test_a_pull_request_from_the_webhook_is_not_looked_up_again() -> None:
    world = World()

    await world.run_once()

    assert world.pulls.calls == []


async def test_a_fork_run_looks_up_the_pull_request_by_head_and_gets_rules_only() -> None:
    fork = make_run_ref(
        is_fork=True, pr_number=None, head_repo_full_name="drive-by/widgets", head_branch="patch-1"
    )
    world = World(script=[], run=fork)  # an LLM call would fail the test
    world.pulls.set(5000, 15)

    await world.run_once()

    [looked_up] = world.pulls.calls
    assert (looked_up.head_repo_full_name, looked_up.head_branch) == ("drive-by/widgets", "patch-1")
    [item] = (await world.record()).items
    assert item.published.source == "rules"
    assert item.published.fallback_reason == "fork_policy"
    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert FALLBACK_TEXT["fork_policy"] in check.output.summary
    assert world.llm.calls == []
    [comment] = world.reporter.comments.values()
    assert "pr=15" in comment.body.splitlines()[0]


async def test_a_zero_budget_publishes_the_rules_diagnosis_and_says_why() -> None:
    world = World(script=[], daily_budget=Decimal(0))

    await world.run_once()

    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert FALLBACK_TEXT["budget"] == "LLM daily limit reached"
    assert FALLBACK_TEXT["budget"] in check.output.summary
    assert world.llm.calls == []


async def test_an_llm_error_publishes_the_rules_diagnosis() -> None:
    world = World(script=[Fail(LLMError("401 invalid API key"))])

    await world.run_once()

    [item] = (await world.record()).items
    assert (item.published.source, item.published.fallback_reason) == ("rules", "llm_error")
    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert FALLBACK_TEXT["llm_error"] in check.output.summary
    assert check.status == "completed"


async def test_no_llm_client_publishes_the_rules_diagnosis_as_disabled() -> None:
    world = World(script=[], with_llm=False)

    await world.run_once()

    [item] = (await world.record()).items
    assert item.published.fallback_reason == "disabled"
    assert world.ledger.usage(world.clock.now().date(), 1).calls == 0


async def test_the_kill_switch_in_the_config_disables_the_llm() -> None:
    world = World(script=[], config=DiagnoseConfig(llm_enabled=False))

    await world.run_once()

    [item] = (await world.record()).items
    assert item.published.fallback_reason == "disabled"
    assert world.llm.calls == []


async def test_at_most_three_jobs_are_diagnosed_at_the_same_time() -> None:
    counting = CountingLLM(answers=5)
    world = World(script=[], llm=counting, config=DiagnoseConfig(max_jobs=5))
    world.logs.add_jobs(world.run, [workflow_job(job_id) for job_id in range(21, 26)])
    for job_id in range(21, 26):
        world.logs.add_log(job_id, log_text())

    await world.run_once()

    assert len((await world.record()).items) == 5
    assert counting.peak == 3


# --- no failed job and the logs -------------------------------------------------------------


async def test_a_run_without_a_failed_job_gets_the_neutral_could_not_diagnose() -> None:
    world = World(jobs=False)
    world.logs.add_jobs(world.run, [workflow_job(1, "success"), workflow_job(2, "cancelled")])

    await world.run_once()  # finishes normally: the job is done

    [check] = world.reporter.checks.values()
    assert check.status == "completed"
    assert check.output is not None
    assert check.output.title == "Could not diagnose"
    assert COULD_NOT_DIAGNOSE["no_failed_job"] in check.output.summary
    assert world.reporter.comments == {}
    assert world.logs.downloads == 0
    assert world.pulls.calls == []
    assert (await world.record()).items == []  # a failure-less attempt is a valid checkpoint


async def test_a_retry_of_a_run_without_a_failed_job_does_not_list_the_jobs_again() -> None:
    world = World(jobs=False)
    world.logs.add_jobs(world.run, [workflow_job(1, "success")])
    world.reporter.fail_next("complete_check", TransientError("502"))
    with pytest.raises(TransientError):
        await world.run_once()

    await world.run_once()

    assert [call for call in world.logs.calls if call[0] == "jobs"] == [("jobs", 5000, 1)]
    [check] = world.reporter.checks.values()
    assert check.status == "completed"


@pytest.mark.parametrize(
    ("status", "after_completed", "transient"),
    [
        (404, timedelta(seconds=5), True),
        (404, timedelta(minutes=9, seconds=59), True),
        (404, timedelta(minutes=10, seconds=1), False),
        (410, timedelta(seconds=5), False),
        (None, timedelta(seconds=5), False),
    ],
)
async def test_logs_that_are_gone_are_transient_only_just_after_the_run(
    status: int | None, after_completed: timedelta, transient: bool
) -> None:
    world = World()
    world.logs.fail_log(11, LogsUnavailable("gone", status_code=status))
    world.clock.advance(after_completed - timedelta(seconds=5))

    expected = TransientError if transient else PermanentError
    with pytest.raises(expected) as excinfo:
        await world.run_once()

    [check] = world.reporter.checks.values()
    if transient:
        assert isinstance(excinfo.value, TransientError)
        assert excinfo.value.retry_after == timedelta(seconds=30)
        assert check.status == "in_progress"  # nothing was given up
        assert await world.store.run_analysis(5000, 1) is None
    else:
        assert check.status == "completed"
        assert check.output is not None
        assert check.output.title == "Could not diagnose"
        assert COULD_NOT_DIAGNOSE["logs_expired"] in check.output.summary
        assert world.llm.calls == []
    assert world.reporter.comments == {}


async def test_a_log_that_shows_up_after_the_grace_retry_is_diagnosed() -> None:
    world = World()
    world.logs.fail_log(11, LogsUnavailable("not yet", status_code=404))
    with pytest.raises(TransientError):
        await world.run_once()

    world.logs.fail_log(11, None)
    await world.run_once()

    [check] = world.reporter.checks.values()  # the same check run, now completed
    assert check.status == "completed"
    assert check.output is not None
    assert check.output.title.startswith("Likely cause")
    assert world.calls_made() == (1, 2, 1)


async def test_a_permission_error_from_the_log_source_publishes_permission() -> None:
    world = World()
    world.logs.fail_jobs(GitHubPermanentError("GET jobs: 403", status_code=403))

    with pytest.raises(PermanentError):
        await world.run_once()

    [check] = world.reporter.checks.values()
    assert check.output is not None
    assert COULD_NOT_DIAGNOSE["permission"] in check.output.summary


async def test_another_permanent_error_publishes_nothing_by_itself() -> None:
    world = World()
    world.logs.fail_jobs(GitHubPermanentError("GET jobs: 422", status_code=422))

    with pytest.raises(PermanentError):
        await world.run_once()

    [check] = world.reporter.checks.values()
    assert check.status == "in_progress"  # the handler decides (D-74)


# --- the central guarantee: retry from what is stored ---------------------------------------


@pytest.mark.parametrize("failure", ["complete_check", "upsert_comment", "save_comment"])
async def test_a_retry_after_a_failure_while_publishing_calls_nothing_again(failure: str) -> None:
    world = World()
    if failure == "save_comment":
        world.store.fail_comment_saves = 1
    else:
        world.reporter.fail_next(failure, TransientError("502 from GitHub"))

    with pytest.raises(TransientError):
        await world.run_once()
    calls_before = world.calls_made()
    log_calls_before = list(world.logs.calls)
    [check_id] = world.reporter.checks
    stored_check = await world.store.publication(world.check_key())
    assert stored_check is not None
    assert stored_check.external_id == check_id  # stored at once

    await world.run_once()  # the retry of the job

    assert world.calls_made() == calls_before == (1, 1, 1)  # no LLM, no download, no reservation
    assert world.logs.calls == log_calls_before  # not even the job listing
    assert world.pulls.calls == []
    [check] = world.reporter.checks.values()
    assert check.id == check_id
    assert check.status == "completed"
    [comment] = world.reporter.comments.values()  # one comment, whichever step failed
    assert comment.body.splitlines()[0] == MARKER
    comment_pub = await world.store.publication(world.comment_key())
    assert comment_pub is not None
    assert comment_pub.external_id == comment.id
    starts = [call for call in world.reporter.calls if call.method == "start_check"]
    assert [call.known_id for call in starts] == [None, check_id]  # the retry reuses the id


async def test_the_retry_publishes_the_same_text_it_would_have_published_first() -> None:
    world = World()
    world.reporter.fail_next("upsert_comment", TransientError("502"))
    with pytest.raises(TransientError):
        await world.run_once()

    await world.run_once()

    first, second = world.reporter.comment_bodies
    assert first == second


async def test_an_older_attempt_does_nothing_when_a_newer_one_published() -> None:
    world = World()
    newer = make_run_ref(run_attempt=2)
    await world.store.save_publication(
        Publication(key=world.check_key(), external_id=9001, run_id=5000, attempt=2)
    )

    await world.run_once()  # attempt 1

    assert world.reporter.calls == []
    assert world.logs.calls == []
    assert world.llm.calls == []
    stored = await world.store.publication(world.check_key())
    assert stored is not None
    assert stored.attempt == newer.run_attempt


async def test_a_new_attempt_edits_the_same_check_and_the_same_comment() -> None:
    world = World(script=[valid(), valid()])
    await world.run_once()
    second = make_run_ref(run_attempt=2)
    world.add_failed_job(second, 21)

    await world.run_once(second)

    [check] = world.reporter.checks.values()
    assert check.status == "completed"
    [comment] = world.reporter.comments.values()
    assert "Attempt 2" in comment.body
    assert comment.edits == 1
    pub = await world.store.publication(world.comment_key())
    assert pub is not None
    assert (pub.run_id, pub.attempt) == (5000, 2)
    assert (await world.record(second)).attempt.attempt == 2


async def test_two_runs_of_the_same_pull_request_and_workflow_make_one_comment() -> None:
    world = World(script=[valid(), valid()])
    other = make_run_ref(run_id=5001)
    world.add_failed_job(other, 31)

    await asyncio.gather(world.run_once(), world.run_once(other))

    assert len(world.reporter.checks) == 2  # one check run per run
    assert len(world.reporter.comments) == 1
    pub = await world.store.publication(world.comment_key())
    assert pub is not None
    assert pub.run_id == 5001  # the newer run owns the comment


# --- rows, secrets and logging --------------------------------------------------------------


async def test_the_installation_and_the_repository_are_inserted_when_missing() -> None:
    world = World()
    assert await world.installations.installation(1) is None

    await world.run_once()

    installation = await world.installations.installation(1)
    assert installation == Installation(id=1, account_login="acme", account_type="Organization")
    repository = await world.installations.repository(100)
    assert repository is not None
    assert (repository.full_name, repository.private, repository.default_branch) == (
        "acme/widgets",
        True,
        "main",
    )


async def test_a_deleted_installation_is_not_overwritten() -> None:
    world = World()
    deleted = Installation(
        id=1, account_login="old", account_type="User", suspended_at=T0, deleted_at=T0
    )
    await world.installations.save_installation(deleted)

    await world.run_once()

    assert await world.installations.installation(1) == deleted
    assert await world.installations.repository(100) is not None


async def test_a_job_without_account_needs_a_known_installation() -> None:
    world = World()

    with pytest.raises(PermanentError, match="unknown"):
        await world.run_once(account_login=None, account_type=None)
    assert world.reporter.calls == []

    await world.installations.save_installation(
        Installation(id=1, account_login="acme", account_type="Organization")
    )
    await world.run_once(account_login=None, account_type=None)

    assert len(world.reporter.checks) == 1


async def test_planted_secrets_never_reach_the_reporter_or_the_store() -> None:
    planted = planted_secrets()
    world = World()
    world.add_failed_job(world.run, 11, extra=[item.line for item in planted])

    await world.run_once()

    record = await world.record()
    assert record.items[0].failure.excerpt.redactions > 0  # they were in the excerpt's source
    published = [
        *world.reporter.comment_bodies,
        *(
            f"{c.output.title}\n{c.output.summary}"
            for c in world.reporter.checks.values()
            if c.output
        ),
        record.model_dump_json(),
        *(repr(request) for request in world.llm.calls),
        *(message.system + message.user for message in world.llm.messages),
    ]
    for item in planted:
        assert all(item.secret not in text for text in published), item.kind


async def test_each_publication_logs_one_line_without_log_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World()

    with caplog.at_level(logging.INFO, logger="cicd_doctor.core.usecases.diagnose_failed_run"):
        await world.run_once()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("published")]
    assert lines == [
        "published check run_id=5000 attempt=1 seconds_since_completed=5.0",
        "published comment run_id=5000 attempt=1 seconds_since_completed=5.0",
    ]
    assert "ModuleNotFoundError" not in caplog.text


# --- could not diagnose for the handler -----------------------------------------------------


async def test_the_handler_can_publish_could_not_diagnose_before_anything_started() -> None:
    world = World()

    published = await world.usecase.publish_could_not_diagnose(world.run, "internal_error")

    assert published
    [check] = world.reporter.checks.values()
    assert check.status == "completed"
    assert check.output is not None
    assert COULD_NOT_DIAGNOSE["internal_error"] in check.output.summary
    stored = await world.store.publication(world.check_key())
    assert stored is not None
    assert stored.external_id == check.id


async def test_could_not_diagnose_respects_a_newer_attempt() -> None:
    world = World()
    await world.store.save_publication(
        Publication(key=world.check_key(), external_id=9001, run_id=5000, attempt=3)
    )

    published = await world.usecase.publish_could_not_diagnose(world.run, "internal_error")

    assert not published
    assert world.reporter.calls == []
