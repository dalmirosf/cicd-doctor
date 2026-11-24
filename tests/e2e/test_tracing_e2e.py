"""A real diagnosis traced in Langfuse (P5 (b), D-118), offline: the signed webhook, the real
worker and GitHub adapters over ``FakeGitHub``, and the Langfuse tracer over a fake SDK client.

The log carries a planted fake secret and so does the model's answer: whatever reaches the SDK
must have neither, and the trace must carry the delivery of the webhook.
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
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.adapters.observability.langfuse_tracer import LangfuseTracer
from tests.diagnose_world import LOG_BODY, DiagnoseWorld, diagnose_world, log_bytes
from tests.e2e.test_diagnose_e2e import deliver, run_job
from tests.redaction_corpus import planted_secrets
from tests.unit.test_tracing import FakeLangfuse

DELIVERY = "b1c2d3e4-0000-4000-8000-0000000000bb"
SECRETS = planted_secrets()
IN_LOG, IN_ANSWER = SECRETS[0], SECRETS[2]


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def sdk() -> FakeLangfuse:
    return FakeLangfuse()


@pytest.fixture
def tracer(sdk: FakeLangfuse) -> LangfuseTracer:
    return LangfuseTracer(sdk, propagate=sdk.propagate)


@pytest.fixture
async def world(tracer: LangfuseTracer) -> AsyncIterator[DiagnoseWorld]:
    clock = FakeClock(datetime.fromisoformat("2026-11-12T12:03:30+00:00"))
    async with diagnose_world(
        clock=clock,
        queue=InMemoryJobQueue(clock),
        store=InMemoryFailureStore(clock),
        ledger=InMemoryUsageLedger(clock),
        installations=InMemoryInstallationStore(),
        llm_script=[valid(probable_cause=f"the answer repeats {IN_ANSWER.line}")],
        tracer=tracer,
    ) as built:
        yield built


async def test_one_diagnosis_is_one_redacted_trace_with_the_delivery(
    world: DiagnoseWorld, sdk: FakeLangfuse, tracer: LangfuseTracer
) -> None:
    body = [*LOG_BODY[:-1], f"env: {IN_LOG.line}", LOG_BODY[-1]]
    world.add_run(body=log_bytes(body))

    assert deliver(world, delivery=DELIVERY) == 202
    assert await run_job(world)
    await tracer.aclose(2)

    [observation] = sdk.observations
    assert observation["as_type"] == "generation"
    assert observation["metadata"]["delivery_id"] == DELIVERY
    assert observation["metadata"]["run_id"] == "5000"
    assert observation["version"] == "diag-v3"
    assert observation["usage_details"]["total"] > 0
    sent = json.dumps([sdk.observations, sdk.propagated], default=str)
    assert IN_LOG.secret not in sent
    assert IN_ANSWER.secret not in sent
