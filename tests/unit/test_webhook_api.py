"""``POST /webhooks/github`` with an in-memory queue: every status code of SPEC §8 (D-56)."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import InMemoryJobQueue
from cicd_doctor.api import webhooks as webhook_module
from cicd_doctor.api.main import create_app
from cicd_doctor.core.errors import TransientError
from cicd_doctor.core.models import Delivery, Job, JobKind, NewJob
from tests.factories import T0
from tests.webhooks import SECRET, encode, headers, load, memory_dependencies

URL = "/webhooks/github"
LEASE = timedelta(minutes=5)


class Harness:
    def __init__(self, *, secrets: tuple[bytes, ...] = (SECRET,)) -> None:
        self.queue = InMemoryJobQueue(FakeClock(T0))
        self.client = TestClient(create_app(memory_dependencies(queue=self.queue, secrets=secrets)))

    def post(self, event: str, payload: dict[str, Any], **options: Any) -> Any:
        body = encode(payload)
        return self.client.post(URL, content=body, headers=headers(event, body, **options))

    async def jobs(self) -> list[Job]:
        leased = []
        while (job := await self.queue.lease("test", LEASE)) is not None:
            leased.append(job)
        return leased


@pytest.fixture
def api() -> Harness:
    return Harness()


# --- accepted and duplicates (FR-02, FR-04) -----------------------------------------------


async def test_a_failed_run_is_accepted_and_enqueued(api: Harness) -> None:
    delivery = str(uuid4())

    response = api.post("workflow_run", load("workflow_run_completed_failure"), delivery=delivery)

    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    [job] = await api.jobs()
    assert (job.kind, job.dedupe_key) == (JobKind.DIAGNOSE_RUN, "diagnose:5000:1")
    assert list(api.queue.deliveries) == [delivery]
    stored = api.queue.deliveries[delivery]
    assert (stored.event, stored.action, stored.installation_id) == (
        "workflow_run",
        "completed",
        10,
    )


async def test_the_same_delivery_three_times_creates_one_job(api: Harness) -> None:
    delivery = str(uuid4())
    payload = load("workflow_run_completed_failure")

    codes = [api.post("workflow_run", payload, delivery=delivery).status_code for _ in range(3)]

    assert codes == [202, 200, 200]
    assert len(api.queue.deliveries) == 1
    assert len(await api.jobs()) == 1


async def test_another_delivery_of_the_same_run_attempt_is_a_duplicate(api: Harness) -> None:
    payload = load("workflow_run_completed_failure")

    first = api.post("workflow_run", payload)
    second = api.post("workflow_run", payload)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json() == {"status": "duplicate"}
    assert len(await api.jobs()) == 1


async def test_an_installation_event_is_enqueued_for_the_worker(api: Harness) -> None:
    response = api.post("installation", load("installation_created"))

    assert response.status_code == 202
    [job] = await api.jobs()
    assert job.kind is JobKind.SYNC_INSTALLATION


# --- nothing stored -----------------------------------------------------------------------


def test_ping_answers_pong_and_stores_nothing(api: Harness) -> None:
    response = api.post("ping", load("ping"))

    assert (response.status_code, response.json()) == (200, {"status": "pong"})
    assert api.queue.deliveries == {}


@pytest.mark.parametrize(
    ("event", "fixture", "change"),
    [
        ("workflow_run", "workflow_run_completed_failure", {"conclusion": "success"}),
        ("workflow_run", "workflow_run_completed_failure", {"action": "requested"}),
        ("issue_comment", "issue_comment_created", {}),
        ("push", "ping", {}),
    ],
)
def test_unhandled_events_answer_204_and_store_nothing(
    api: Harness, event: str, fixture: str, change: dict[str, str]
) -> None:
    payload = load(fixture)
    if "conclusion" in change:
        payload["workflow_run"]["conclusion"] = change["conclusion"]
    if "action" in change:
        payload["action"] = change["action"]

    response = api.post(event, payload)

    assert (response.status_code, response.content) == (204, b"")
    assert api.queue.deliveries == {}


# --- signature (FR-01) --------------------------------------------------------------------


@pytest.mark.parametrize("signature", [None, "sha256=" + "0" * 64, "garbage"])
async def test_a_missing_or_wrong_signature_is_401_and_stores_nothing(
    api: Harness, signature: str | None
) -> None:
    body = encode(load("workflow_run_completed_failure"))
    sent = headers("workflow_run", body)
    if signature is None:
        del sent["X-Hub-Signature-256"]
    else:
        sent["X-Hub-Signature-256"] = signature

    response = api.client.post(URL, content=body, headers=sent)

    assert (response.status_code, response.json()) == (401, {"detail": "invalid signature"})
    assert api.queue.deliveries == {}
    assert await api.jobs() == []


def test_a_body_signed_with_another_secret_is_401(api: Harness) -> None:
    response = api.post("workflow_run", load("workflow_run_completed_failure"), secret=b"other")

    assert response.status_code == 401


def test_the_previous_secret_is_accepted_while_rotating() -> None:
    rotating = Harness(secrets=(SECRET, b"previous"))

    response = rotating.post(
        "workflow_run", load("workflow_run_completed_failure"), secret=b"previous"
    )

    assert response.status_code == 202


def test_the_signature_is_checked_before_the_headers(api: Harness) -> None:
    response = api.client.post(URL, content=b"{}")  # no headers at all

    assert response.status_code == 401


def test_no_configured_secret_is_503() -> None:
    unconfigured = Harness(secrets=())

    response = unconfigured.post("workflow_run", load("workflow_run_completed_failure"))

    assert response.status_code == 503
    assert unconfigured.queue.deliveries == {}


# --- size (413) ---------------------------------------------------------------------------


def test_a_declared_body_over_the_limit_is_413(
    api: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_module, "MAX_BODY_BYTES", 100)

    response = api.post("workflow_run", load("workflow_run_completed_failure"))

    assert response.status_code == 413
    assert api.queue.deliveries == {}


def test_a_chunked_body_over_the_limit_is_413(
    api: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_module, "MAX_BODY_BYTES", 100)
    body = encode(load("workflow_run_completed_failure"))
    sent = headers("workflow_run", body)

    # An iterator makes the client stream the body without a Content-Length.
    response = api.client.post(URL, content=iter([body[:80], body[80:]]), headers=sent)

    assert response.status_code == 413


# --- bad requests (400) -------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["X-GitHub-Event", "X-GitHub-Delivery"])
def test_missing_github_headers_are_400(api: Harness, missing: str) -> None:
    body = encode(load("workflow_run_completed_failure"))
    sent = headers("workflow_run", body)
    del sent[missing]

    response = api.client.post(URL, content=body, headers=sent)

    assert response.status_code == 400
    assert missing in response.json()["detail"]


def test_a_delivery_id_that_is_not_a_uuid_is_400(api: Harness) -> None:
    response = api.post(
        "workflow_run", load("workflow_run_completed_failure"), delivery="not-a-uuid"
    )

    assert response.status_code == 400


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe"])
def test_a_signed_body_that_is_not_a_json_object_is_400(api: Harness, body: bytes) -> None:
    response = api.client.post(URL, content=body, headers=headers("workflow_run", body))

    assert response.status_code == 400
    assert api.queue.deliveries == {}


def test_a_handled_event_missing_a_field_is_400(api: Harness) -> None:
    payload = load("workflow_run_completed_failure")
    del payload["workflow_run"]["run_attempt"]

    response = api.post("workflow_run", payload)

    assert response.status_code == 400
    assert "workflow_run.run_attempt" in response.json()["detail"]
    assert api.queue.deliveries == {}


# --- database down (503) ------------------------------------------------------------------


class UnavailableQueue(InMemoryJobQueue):
    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        raise TransientError("database unavailable: OperationalError")


def test_a_database_outage_is_503_so_the_delivery_can_be_redelivered() -> None:
    client = TestClient(create_app(memory_dependencies(queue=UnavailableQueue())))
    body = encode(load("workflow_run_completed_failure"))

    response = client.post(URL, content=body, headers=headers("workflow_run", body))

    assert response.status_code == 503
