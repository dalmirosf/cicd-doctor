"""``POST /webhooks/github`` and ``GET /ready`` against Postgres (SPEC §13 P1a, FR-01 to FR-04)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.postgres.installations import PostgresInstallationStore
from cicd_doctor.adapters.postgres.queue import PostgresJobQueue
from cicd_doctor.api.deps import ApiDependencies
from cicd_doctor.api.main import create_app
from cicd_doctor.core.models import JobKind
from cicd_doctor.worker import Worker
from cicd_doctor.worker.handlers import sync_installation_handler
from tests.pg import scalar
from tests.webhooks import SECRET, encode, headers, load

URL = "/webhooks/github"
UNREACHABLE = "postgresql+psycopg://cicd:cicd@127.0.0.1:1/nowhere"  # nothing listens on port 1


@dataclass
class Api:
    client: httpx2.AsyncClient
    queue: PostgresJobQueue
    url: str

    def count(self, table: str) -> int:
        return int(str(scalar(self.url, f"SELECT count(*) FROM {table}")))


@pytest.fixture
async def api(pg_clean: str) -> AsyncIterator[Api]:
    clock = SystemClock()
    async with PostgresJobQueue.connect(pg_clean, clock, listen=False) as queue:
        app = create_app(
            ApiDependencies(queue=queue, webhook_secrets=(SECRET,), ready=queue.ping, clock=clock)
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Api(client, queue, pg_clean)


async def post(
    api: Api, event: str, fixture: str, *, delivery: str | None = None
) -> httpx2.Response:
    body = encode(load(fixture))
    return await api.client.post(URL, content=body, headers=headers(event, body, delivery=delivery))


@pytest.mark.parametrize("signature", [None, "sha256=" + "0" * 64])
async def test_a_missing_or_wrong_signature_is_401_and_creates_no_rows(
    api: Api, signature: str | None
) -> None:
    body = encode(load("workflow_run_completed_failure"))
    sent = headers("workflow_run", body)
    if signature is None:
        del sent["X-Hub-Signature-256"]
    else:
        sent["X-Hub-Signature-256"] = signature

    response = await api.client.post(URL, content=body, headers=sent)

    assert response.status_code == 401
    assert (api.count("webhook_deliveries"), api.count("jobs")) == (0, 0)


async def test_the_same_delivery_posted_three_times_creates_one_row_and_one_job(
    api: Api,
) -> None:
    delivery = str(uuid4())

    codes = [
        (
            await post(api, "workflow_run", "workflow_run_completed_failure", delivery=delivery)
        ).status_code
        for _ in range(3)
    ]

    assert codes == [202, 200, 200]
    assert (api.count("webhook_deliveries"), api.count("jobs")) == (1, 1)
    assert scalar(api.url, "SELECT dedupe_key FROM jobs") == "diagnose:5000:1"
    assert scalar(api.url, "SELECT delivery_id::text FROM jobs") == delivery


async def test_another_delivery_of_the_same_attempt_records_the_delivery_but_no_new_job(
    api: Api,
) -> None:
    first = await post(api, "workflow_run", "workflow_run_completed_failure")
    second = await post(api, "workflow_run", "workflow_run_completed_failure")

    assert (first.status_code, second.status_code) == (202, 200)
    assert (api.count("webhook_deliveries"), api.count("jobs")) == (2, 1)


async def test_ping_and_unhandled_events_store_nothing(api: Api) -> None:
    ping = await post(api, "ping", "ping")
    comment = await post(api, "issue_comment", "issue_comment_created")

    assert (ping.status_code, comment.status_code) == (200, 204)
    assert (api.count("webhook_deliveries"), api.count("jobs")) == (0, 0)


async def test_the_job_payload_has_no_raw_github_fields(api: Api) -> None:
    await post(api, "workflow_run", "workflow_run_completed_failure")

    payload = str(scalar(api.url, "SELECT payload::text FROM jobs"))

    assert "head_commit" not in payload
    assert "example.com" not in payload


async def test_an_installation_event_reaches_the_tables_through_the_worker(api: Api) -> None:
    response = await post(api, "installation", "installation_created")
    assert response.status_code == 202

    async with PostgresInstallationStore.connect(api.url) as store:
        worker = Worker(
            api.queue,
            {JobKind.SYNC_INSTALLATION: sync_installation_handler(store)},
            SystemClock(),
            lease=timedelta(seconds=30),
            name="it",
        )
        assert await worker.run_one("it:0") is True
        installation = await store.installation(10)
        widgets = await store.repository(100)

    assert installation is not None
    assert (installation.account_login, installation.account_type) == ("acme", "Organization")
    assert widgets is not None
    assert (widgets.full_name, widgets.private, widgets.default_branch) == (
        "acme/widgets",
        True,
        None,
    )
    assert api.count("repositories") == 2
    assert scalar(api.url, "SELECT status::text FROM jobs") == "done"


async def test_ready_is_200_with_the_database_up(api: Api) -> None:
    response = await api.client.get("/ready")

    assert (response.status_code, response.json()) == (200, {"status": "ready"})


async def test_ready_is_503_and_webhooks_are_503_when_the_database_is_unreachable(
    pg_url: str,
) -> None:
    clock = SystemClock()
    async with PostgresJobQueue.connect(UNREACHABLE, clock, listen=False) as queue:
        app = create_app(
            ApiDependencies(
                queue=queue,
                webhook_secrets=(SECRET,),
                ready=queue.ping,
                clock=clock,
                enqueue_timeout=timedelta(seconds=1),
            )
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            ready = await client.get("/ready")
            body = encode(load("workflow_run_completed_failure"))
            webhook = await client.post(URL, content=body, headers=headers("workflow_run", body))

    assert (ready.status_code, ready.json()) == (503, {"status": "unavailable"})
    assert webhook.status_code == 503
