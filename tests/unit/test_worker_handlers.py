import asyncio
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.github.auth import InstallationTokenRefused
from cicd_doctor.adapters.github.http import GitHubPermanentError
from cicd_doctor.adapters.llm.openai_client import OpenAILLMClient
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
)
from cicd_doctor.app import container
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.errors import LogsUnavailable, PermanentError, TransientError
from cicd_doctor.core.models import (
    DiagnoseRunPayload,
    Installation,
    InstallationAction,
    InstallationEvent,
    Job,
    JobKind,
    JobStatus,
    RecordOutcomePayload,
    RunRef,
)
from cicd_doctor.core.usecases.purge import PurgeConfig
from cicd_doctor.worker.handlers import (
    diagnose_run_handler,
    purge_handler,
    record_outcome_handler,
    sync_installation_handler,
)
from tests.factories import T0, make_run_analysis, make_run_ref


async def noop(job: Job) -> None:
    return None


def job(payload: dict[str, Any]) -> Job:
    return Job(
        id=1,
        kind=JobKind.SYNC_INSTALLATION,
        payload=payload,
        dedupe_key=None,
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        run_after=T0,
        locked_until=None,
        locked_by="w",
        last_error=None,
    )


async def test_sync_installation_applies_the_event() -> None:
    store = InMemoryInstallationStore()
    event = InstallationEvent(
        action=InstallationAction.CREATED,
        installation=Installation(id=10, account_login="acme", account_type="Organization"),
        occurred_at=T0,
    )

    await sync_installation_handler(store)(job(event.model_dump(mode="json")))

    assert await store.installation(10) == event.installation


async def test_record_outcome_validates_the_payload_and_runs_the_use_case() -> None:
    seen: list[RecordOutcomePayload] = []

    class Recorder:
        async def run(self, payload: RecordOutcomePayload) -> None:
            seen.append(payload)

    payload = RecordOutcomePayload(
        run=make_run_ref(),
        completed_at=T0,
        default_branch="main",
        private=False,
        account_login="acme",
        account_type="Organization",
    )

    await record_outcome_handler(Recorder())(job(payload.model_dump(mode="json")))

    assert seen == [payload]


async def test_record_outcome_with_a_malformed_payload_is_permanent_and_publishes_nothing() -> None:
    class Boom:
        async def run(self, payload: RecordOutcomePayload) -> None:
            raise AssertionError("must not run")

    with pytest.raises(PermanentError, match="invalid record_outcome payload"):
        await record_outcome_handler(Boom())(job({"run": {}}))


async def test_a_malformed_payload_is_a_permanent_error() -> None:
    with pytest.raises(PermanentError, match="invalid sync_installation payload"):
        await sync_installation_handler(InMemoryInstallationStore())(job({"action": "nope"}))


def test_every_kind_but_explain_has_a_real_handler() -> None:
    registry = container.handlers(InMemoryInstallationStore(), noop, noop, noop)

    assert set(registry) == set(JobKind)
    real = {
        kind for kind, handler in registry.items() if handler is not container.placeholder_handler
    }
    assert real == {
        JobKind.SYNC_INSTALLATION,
        JobKind.DIAGNOSE_RUN,
        JobKind.PURGE,
        JobKind.RECORD_OUTCOME,
    }
    assert registry[JobKind.DIAGNOSE_RUN] is noop


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        ("now", None, (b"now",)),
        ("now", "before", (b"now", b"before")),
        (None, None, ()),
    ],
)
def test_webhook_secrets_list_the_current_secret_first(
    current: str | None, previous: str | None, expected: tuple[bytes, ...]
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://u@127.0.0.1/db"),
        github_webhook_secret=SecretStr(current) if current else None,
        github_webhook_secret_previous=SecretStr(previous) if previous else None,
    )

    assert container.webhook_secrets(settings) == expected


# --- diagnose_run (D-74) ---------------------------------------------------------------------


class StubUseCase:
    """The use case as the handler sees it: it fails on demand and records what it publishes."""

    def __init__(
        self, error: Exception | None = None, publish_error: Exception | None = None
    ) -> None:
        self.error = error
        self.publish_error = publish_error
        self.ran: list[DiagnoseRunPayload] = []
        self.published: list[tuple[RunRef, str, datetime | None]] = []
        self.only_if_started: list[bool] = []
        self.hang = False

    async def run(self, payload: DiagnoseRunPayload) -> None:
        self.ran.append(payload)
        if self.error is not None:
            raise self.error

    async def publish_could_not_diagnose(
        self,
        run: RunRef,
        reason: str,
        completed_at: datetime | None = None,
        *,
        only_if_started: bool = False,
    ) -> bool:
        self.only_if_started.append(only_if_started)
        if self.hang:
            await asyncio.sleep(3600)
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((run, reason, completed_at))
        return True


def make_payload() -> DiagnoseRunPayload:
    return DiagnoseRunPayload(
        run=make_run_ref(),
        conclusion="failure",
        completed_at=T0,
        default_branch="main",
        private=True,
        account_login="acme",
        account_type="Organization",
    )


def diagnose_job(*, attempts: int = 1, max_attempts: int = 5, payload: Any = None) -> Job:
    body = make_payload().model_dump(mode="json") if payload is None else payload
    return job(body).model_copy(
        update={"kind": JobKind.DIAGNOSE_RUN, "attempts": attempts, "max_attempts": max_attempts}
    )


async def test_a_successful_run_publishes_nothing_extra() -> None:
    use_case = StubUseCase()

    await diagnose_run_handler(use_case)(diagnose_job())

    assert len(use_case.ran) == 1
    assert use_case.published == []


async def test_an_invalid_payload_is_permanent_and_never_reaches_the_use_case() -> None:
    use_case = StubUseCase()

    with pytest.raises(PermanentError, match="invalid diagnose_run payload"):
        await diagnose_run_handler(use_case)(diagnose_job(payload={"run": "nope"}))

    assert use_case.ran == []
    assert use_case.published == []


async def test_the_last_attempt_publishes_internal_error_and_reraises_the_original() -> None:
    use_case = StubUseCase(error=TransientError("GitHub is down"))

    with pytest.raises(TransientError, match="GitHub is down"):
        await diagnose_run_handler(use_case)(diagnose_job(attempts=5, max_attempts=5))

    assert [(run.run_id, reason) for run, reason, _ in use_case.published] == [
        (5000, "internal_error")
    ]
    assert use_case.published[0][2] == T0


async def test_an_unexpected_error_on_the_last_attempt_is_reported_too() -> None:
    use_case = StubUseCase(error=RuntimeError("bug"))

    with pytest.raises(RuntimeError, match="bug"):
        await diagnose_run_handler(use_case)(diagnose_job(attempts=3, max_attempts=3))

    assert [reason for _, reason, _ in use_case.published] == ["internal_error"]


@pytest.mark.parametrize(("conclusion", "expected"), [("failure", False), ("cancelled", True)])
async def test_a_cancelled_run_only_gives_up_on_a_check_that_already_started(
    conclusion: str, expected: bool
) -> None:
    use_case = StubUseCase(error=PermanentError("annotations: 404"))
    body = make_payload().model_copy(update={"conclusion": conclusion})

    with pytest.raises(PermanentError):
        await diagnose_run_handler(use_case)(diagnose_job(payload=body.model_dump(mode="json")))

    assert use_case.only_if_started == [expected]


async def test_a_transient_error_before_the_last_attempt_publishes_nothing() -> None:
    use_case = StubUseCase(error=TransientError("rate limited"))

    with pytest.raises(TransientError):
        await diagnose_run_handler(use_case)(diagnose_job(attempts=4, max_attempts=5))

    assert use_case.published == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LogsUnavailable("gone", status_code=410), "logs_expired"),
        (GitHubPermanentError("GET logs: 403", status_code=403), "permission"),
        (GitHubPermanentError("GET jobs: 422", status_code=422), "internal_error"),
        (PermanentError("installation 1 is unknown"), "internal_error"),
    ],
)
async def test_a_permanent_error_publishes_its_reason_on_the_first_attempt(
    error: PermanentError, reason: str
) -> None:
    use_case = StubUseCase(error=error)

    with pytest.raises(PermanentError) as excinfo:
        await diagnose_run_handler(use_case)(diagnose_job(attempts=1))

    assert excinfo.value is error
    assert [r for _, r, _ in use_case.published] == [reason]


async def test_a_suspended_installation_publishes_nothing() -> None:
    error = InstallationTokenRefused("installation token refused", status_code=403)
    use_case = StubUseCase(error=error)

    with pytest.raises(InstallationTokenRefused):
        await diagnose_run_handler(use_case)(diagnose_job(attempts=5, max_attempts=5))

    assert use_case.published == []


async def test_an_error_while_publishing_does_not_hide_the_original_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    use_case = StubUseCase(
        error=TransientError("the original"), publish_error=RuntimeError("publishing failed")
    )

    with caplog.at_level(logging.WARNING), pytest.raises(TransientError, match="the original"):
        await diagnose_run_handler(use_case)(diagnose_job(attempts=5, max_attempts=5))

    assert "could not publish" in caplog.text


async def test_publishing_has_its_own_timeout() -> None:
    use_case = StubUseCase(error=TransientError("the original"))
    use_case.hang = True

    async with asyncio.timeout(5):
        with pytest.raises(TransientError, match="the original"):
            await diagnose_run_handler(use_case, give_up_timeout_s=0.05)(
                diagnose_job(attempts=5, max_attempts=5)
            )


async def test_cancellation_while_publishing_is_not_swallowed() -> None:
    use_case = StubUseCase(error=TransientError("the original"))
    use_case.hang = True
    task = asyncio.create_task(
        diagnose_run_handler(use_case)(diagnose_job(attempts=5, max_attempts=5))
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- the container ---------------------------------------------------------------------------


def settings_with(**changes: Any) -> Settings:
    return Settings(
        _env_file=None, database_url=SecretStr("postgresql+psycopg://u@127.0.0.1/db"), **changes
    )


def test_the_llm_client_exists_only_with_both_a_key_and_a_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert container.make_llm_client(settings_with()) is None
        assert (
            container.make_llm_client(settings_with(openai_api_key=SecretStr("sk-key-value")))
            is None
        )
        assert container.make_llm_client(settings_with(openai_model="some-model")) is None
    assert "OPENAI_API_KEY and OPENAI_MODEL not set" in caplog.text
    assert "OPENAI_MODEL not set" in caplog.text
    assert "OPENAI_API_KEY not set" in caplog.text
    assert "sk-key-value" not in caplog.text

    client = container.make_llm_client(
        settings_with(openai_api_key=SecretStr("sk-key-value"), openai_model="some-model")
    )
    assert isinstance(client, OpenAILLMClient)


def test_the_worker_refuses_to_start_without_the_github_app_and_names_only_variables() -> None:
    with pytest.raises(container.ConfigurationError) as excinfo:
        container.require_github_app(settings_with())
    assert "GITHUB_APP_ID and GITHUB_PRIVATE_KEY" in str(excinfo.value)

    only_id = settings_with(github_app_id=424242)
    with pytest.raises(container.ConfigurationError) as excinfo:
        container.require_github_app(only_id)
    assert "GITHUB_PRIVATE_KEY" in str(excinfo.value)
    assert "GITHUB_APP_ID" not in str(excinfo.value)
    assert "424242" not in str(excinfo.value)

    only_key = settings_with(github_private_key=SecretStr("not-printed-secret"))
    with pytest.raises(container.ConfigurationError) as excinfo:
        container.require_github_app(only_key)
    assert "GITHUB_APP_ID" in str(excinfo.value)
    assert "not-printed-secret" not in str(excinfo.value)


def test_serve_worker_exits_with_a_message_that_has_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        container,
        "Settings",
        lambda: settings_with(github_app_id=1, github_private_key=SecretStr("secret-not-a-pem")),
    )

    with pytest.raises(SystemExit) as excinfo:
        container.serve_worker()

    message = str(excinfo.value)
    assert "GITHUB_PRIVATE_KEY" in message
    assert "secret-not-a-pem" not in message


def test_prices_and_limits_come_from_the_settings() -> None:
    settings = settings_with(
        llm_price_input_per_mtok=Decimal("0.4"),
        llm_price_output_per_mtok=Decimal("1.6"),
        llm_enabled=False,
        log_max_jobs=2,
        log_max_bytes=1000,
        excerpt_max_tokens=500,
        cache_ttl_days=3,
    )

    assert container.cost_model(settings) == CostModel(
        input_per_mtok=Decimal("0.4"), output_per_mtok=Decimal("1.6")
    )
    config = container.diagnose_config(settings)
    assert (config.llm_enabled, config.max_jobs, config.max_bytes, config.excerpt_max_tokens) == (
        False,
        2,
        1000,
        500,
    )
    assert config.cache_ttl_days == 3
    assert container.cost_model(settings_with()) == CostModel()


def test_the_embedder_is_built_without_loading_the_model_and_can_be_switched_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    embedder = container.make_embedder(settings_with(embedding_threads=2))
    assert embedder is not None
    assert not embedder.ready  # nothing is loaded or downloaded here (D-78)
    with caplog.at_level(logging.WARNING):
        assert container.make_embedder(settings_with(embeddings_enabled=False)) is None
    assert "EMBEDDINGS_ENABLED=false" in caplog.text
    assert container.start_warming(None) is None


def test_the_warm_up_runs_in_a_daemon_thread_and_does_not_wait_for_the_model() -> None:
    class Slow:
        started = threading.Event()
        release = threading.Event()

        def warm(self) -> bool:
            self.started.set()
            self.release.wait(5)
            return True

    slow = Slow()

    thread = container.start_warming(slow)  # type: ignore[arg-type]

    assert thread is not None
    assert thread.daemon
    assert slow.started.wait(5)
    assert thread.is_alive()  # start_warming returned while the load is still going
    slow.release.set()
    thread.join(5)


async def test_the_purge_handler_runs_the_purge_with_the_clock_and_the_config() -> None:
    clock = FakeClock(T0)
    store = InMemoryFailureStore(clock)
    run = make_run_ref()
    record = make_run_analysis(run)
    old = record.attempt.model_copy(update={"completed_at": T0 - timedelta(days=11)})
    await store.save_run_analysis(record.model_copy(update={"attempt": old}))
    handler = purge_handler(
        InMemoryJobQueue(clock),
        store,
        InMemoryInstallationStore(),
        clock,
        PurgeConfig(failures_days=10),
    )

    await handler(job({}))

    assert await store.run_analysis(run.run_id, run.run_attempt) is None


def test_the_purge_settings_have_the_decided_defaults_and_reach_the_config() -> None:
    settings = Settings(database_url=SecretStr("postgresql+psycopg://x"))

    assert (settings.purge_enabled, settings.purge_hour_utc) == (True, 3)
    assert container.purge_config(settings) == PurgeConfig(
        deliveries_days=30,
        failures_days=90,
        jobs_days=30,
        stale_after_days=30,
        uninstall_grace_days=7,
    )
