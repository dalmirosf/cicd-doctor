"""One ``delivery_id`` followed from the signed webhook to the diagnosis (P5 (a), D-117).

Same offline world as ``test_diagnose_e2e``: the real API and worker, the real GitHub adapters over
``FakeGitHub``. The assertion is on the JSON lines, which is what CloudWatch will hold.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime

import pytest
import structlog

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import (
    InMemoryFailureStore,
    InMemoryInstallationStore,
    InMemoryJobQueue,
    InMemoryUsageLedger,
)
from cicd_doctor.app.logging_config import configure_logging
from tests.diagnose_world import DiagnoseWorld, diagnose_world
from tests.e2e.test_diagnose_e2e import deliver, run_job
from tests.redaction_corpus import planted_secrets

DELIVERY = "b1c2d3e4-0000-4000-8000-0000000000aa"


@pytest.fixture(autouse=True)
def json_logs() -> Iterator[None]:
    import logging

    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
async def world() -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
    ) as built:
        yield built


async def test_every_line_of_a_diagnosis_carries_the_delivery_of_its_webhook(
    world: DiagnoseWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("json")
    world.add_run()

    assert deliver(world, delivery=DELIVERY) == 202
    assert await run_job(world)

    err = capsys.readouterr().err
    records = [json.loads(line) for line in err.splitlines() if line.strip()]
    mine = [r for r in records if r.get("delivery_id") == DELIVERY]
    messages = [str(r["message"]) for r in mine]
    assert messages[0] == "webhook enqueued"
    assert "job started" in messages
    assert "job done" in messages
    assert any(m.startswith("published ") for m in messages)  # the use case, in core/
    assert {r["run_id"] for r in mine} == {5000}
    assert {r["repository"] for r in mine} == {"acme/widgets"}
    api = [r for r in mine if str(r["logger"]).startswith("cicd_doctor.api")]
    worker = [r for r in mine if str(r["logger"]).startswith("cicd_doctor.worker")]
    assert api
    assert worker
    # Every line between the start and the end of the job carries the delivery, whoever wrote it.
    all_messages = [str(r["message"]) for r in records]
    inside = records[all_messages.index("job started") : all_messages.index("job done") + 1]
    assert all(r.get("delivery_id") == DELIVERY for r in inside)
    assert all(planted.secret not in err for planted in planted_secrets())
