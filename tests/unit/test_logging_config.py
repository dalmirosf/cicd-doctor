"""Structured logs (P5 (a), D-117, NFR-10): JSON shape, correlation, redaction, third parties."""

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import pytest
import structlog
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.api.main import create_app
from cicd_doctor.app.logging_config import configure_logging
from cicd_doctor.core.models import Job, JobKind, JobStatus, NewJob
from cicd_doctor.worker import Worker
from cicd_doctor.worker.runner import job_context
from tests.redaction_corpus import planted_secrets
from tests.webhooks import SECRET, encode, headers, load, memory_dependencies

SECRET_VALUE = planted_secrets()[0].secret
T0 = datetime.fromisoformat("2026-11-24T12:00:00+00:00")
DELIVERY = "b1c2d3e4-0000-4000-8000-000000000042"


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]


def test_json_mode_writes_one_object_per_line_for_structlog_and_stdlib(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("json")

    structlog.get_logger("cicd_doctor.api").info("hello", repo_id=7)
    logging.getLogger("cicd_doctor.core.x").warning("from core %d", 3)

    first, second = lines(capsys)
    assert first["message"] == "hello"
    assert first["repo_id"] == 7
    assert first["level"] == "info"
    assert first["logger"] == "cicd_doctor.api"
    assert str(first["timestamp"]).endswith("Z")
    assert (second["message"], second["level"]) == ("from core 3", "warning")


@pytest.mark.parametrize(
    "name", ["huggingface_hub.utils._cache", "uvicorn.error", "uvicorn.access", "httpx2"]
)
def test_third_party_stdlib_loggers_come_out_as_json(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("json")

    logging.getLogger(name).warning("Ignoring corrupted tree cache file")

    (record,) = lines(capsys)
    assert record["message"] == "Ignoring corrupted tree cache file"
    assert record["logger"] == name


def test_http_loggers_stay_silent_at_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")

    logging.getLogger("httpx2").info("GET https://example.test/?sig=1")

    assert lines(capsys) == []


def test_log_level_is_honoured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json", "ERROR")

    logging.getLogger("cicd_doctor.x").warning("quiet")
    logging.getLogger("cicd_doctor.x").error("loud")

    assert [record["message"] for record in lines(capsys)] == ["loud"]


def test_text_mode_is_readable_and_still_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("text")

    structlog.get_logger("cicd_doctor.api").info("hello", note=f"x {SECRET_VALUE}")
    logging.getLogger("cicd_doctor.x").info("plain")

    err = capsys.readouterr().err
    assert "hello" in err
    assert "plain" in err
    assert not err.lstrip().startswith("{")
    assert SECRET_VALUE not in err


def test_configure_twice_does_not_duplicate_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")
    configure_logging("json")

    logging.getLogger("cicd_doctor.x").warning("once")

    assert len(lines(capsys)) == 1


# --- redaction -------------------------------------------------------------------------------


def test_every_string_value_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")
    log = structlog.get_logger("cicd_doctor.x")

    log.info(f"in the event {SECRET_VALUE}", field=f"value {SECRET_VALUE}")
    log.info("nested", data={"a": [f"deep {SECRET_VALUE}"]}, items=(f"t {SECRET_VALUE}",))
    logging.getLogger("cicd_doctor.y").warning("stdlib %s", f"arg {SECRET_VALUE}")
    logging.getLogger("cicd_doctor.y").warning("extra", extra={"note": f"e {SECRET_VALUE}"})

    err = capsys.readouterr().err
    assert err.count("\n") == 4
    assert SECRET_VALUE not in err


def test_a_key_that_names_a_secret_hides_its_value(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")

    structlog.get_logger("cicd_doctor.x").info(
        "call", authorization="plain words", headers={"X-Api-Key": "plain words"}
    )

    (record,) = lines(capsys)
    assert "plain words" not in json.dumps(record)


@pytest.mark.parametrize("use_structlog", [True, False])
def test_exceptions_are_redacted_and_bounded(
    use_structlog: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("json")
    try:
        raise ValueError(f"bad input {SECRET_VALUE} " + "x" * 20_000)
    except ValueError:
        if use_structlog:
            structlog.get_logger("cicd_doctor.x").exception("boom")
        else:
            logging.getLogger("cicd_doctor.x").exception("boom")

    (record,) = lines(capsys)
    exception = str(record["exception"])
    assert "ValueError" in exception
    assert "Traceback" in exception
    assert SECRET_VALUE not in exception
    assert len(exception) < 7000


def test_an_exception_in_the_message_args_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("json")

    logging.getLogger("cicd_doctor.x").error("failed: %s", ValueError(SECRET_VALUE))

    assert SECRET_VALUE not in capsys.readouterr().err


# --- correlation -----------------------------------------------------------------------------


def api_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [r for r in lines(capsys) if str(r["logger"]).startswith("cicd_doctor.api")]


def post_failure(client: TestClient, delivery: str) -> int:
    body = encode(load("workflow_run_completed_failure"))
    response = client.post(
        "/webhooks/github",
        content=body,
        headers=headers("workflow_run", body, delivery=delivery),
    )
    return response.status_code


def test_webhook_lines_carry_the_delivery_and_the_ids_of_the_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("json")
    payload = load("workflow_run_completed_failure")

    with TestClient(create_app(memory_dependencies())) as client:
        assert post_failure(client, DELIVERY) == 202

    (record,) = api_lines(capsys)
    assert record["message"] == "webhook enqueued"
    assert record["delivery_id"] == DELIVERY
    assert record["github_event"] == "workflow_run.completed"
    assert record["run_id"] == payload["workflow_run"]["id"]
    assert record["repository"] == payload["repository"]["full_name"]
    assert record["installation_id"] == payload["installation"]["id"]
    assert structlog.contextvars.get_contextvars() == {}  # nothing leaks into the next request


def test_an_unverified_webhook_logs_no_request_data(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")
    body = encode(load("workflow_run_completed_failure"))
    bad = headers("workflow_run", body, delivery=DELIVERY)
    bad["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    with TestClient(create_app(memory_dependencies())) as client:
        assert client.post("/webhooks/github", content=body, headers=bad).status_code == 401

    (record,) = api_lines(capsys)
    assert "delivery_id" not in record
    assert "repository" not in record
    assert SECRET.decode() not in json.dumps(record)


def make_job(payload: dict[str, object], delivery: str | None = None) -> Job:
    return Job(
        id=9,
        kind=JobKind.DIAGNOSE_RUN,
        payload=payload,
        dedupe_key=None,
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        run_after=T0,
        locked_until=None,
        locked_by=None,
        last_error=None,
        delivery_id=UUID(delivery) if delivery else None,
    )


def test_job_context_reads_only_ids_and_the_repository_name() -> None:
    job = make_job(
        {
            "run": {
                "installation_id": 5,
                "run_id": 77,
                "repo_full_name": "o/r",
                "head_branch": "b",
            },
            "pr_number": 3,
        },
        DELIVERY,
    )

    assert job_context(job) == {
        "job_id": 9,
        "job_kind": "diagnose_run",
        "delivery_id": DELIVERY,
        "installation_id": 5,
        "run_id": 77,
        "repository": "o/r",
    }


def test_job_context_of_a_flat_payload_and_of_garbage() -> None:
    flat = job_context(make_job({"installation_id": 5, "repo_full_name": "o/r", "run": "x"}))
    assert flat == {
        "job_id": 9,
        "job_kind": "diagnose_run",
        "installation_id": 5,
        "repository": "o/r",
    }
    garbage = job_context(make_job({"installation_id": "5", "repo_full_name": 3}))
    assert garbage == {"job_id": 9, "job_kind": "diagnose_run"}


async def test_worker_lines_carry_the_job_fields_and_the_context_is_cleared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("json")
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    with TestClient(create_app(memory_dependencies(queue=queue))) as client:
        assert post_failure(client, DELIVERY) == 202
    capsys.readouterr()

    async def handler(job: Job) -> None:
        logging.getLogger("cicd_doctor.core.usecases.x").info("inside the use case")

    worker = Worker(queue, {JobKind.DIAGNOSE_RUN: handler}, clock, name="t")
    assert await worker.run_one("c")

    records = lines(capsys)
    assert [r["message"] for r in records] == ["job started", "inside the use case", "job done"]
    for record in records:
        assert record["delivery_id"] == DELIVERY
        assert record["job_id"] == 1
        assert record["job_kind"] == "diagnose_run"
        assert record["run_id"]
        assert record["repository"]
        assert record["installation_id"]
    assert structlog.contextvars.get_contextvars() == {}


async def test_a_failing_job_logs_a_redacted_error(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("json")
    clock = FakeClock(T0)
    queue = InMemoryJobQueue(clock)
    await queue.enqueue(NewJob(kind=JobKind.DIAGNOSE_RUN, payload={}), None)

    async def handler(job: Job) -> None:
        raise RuntimeError(f"cannot parse {SECRET_VALUE}")

    worker = Worker(queue, {JobKind.DIAGNOSE_RUN: handler}, clock, name="t")
    await worker.run_one("c")

    err = capsys.readouterr().err
    assert "raised an unexpected error" in err
    assert SECRET_VALUE not in err


def test_nothing_goes_to_stdout_before_logging_is_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unconfigured structlog prints to stdout; ours must not (the crash worker's protocol)."""
    from cicd_doctor.adapters.structured_log import get_logger

    get_logger("cicd_doctor.api").warning("before configure")

    assert capsys.readouterr().out == ""
