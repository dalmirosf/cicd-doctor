"""LLM traces (P5 (b), D-118): one per real model call, none otherwise, and a tracer can never
break or delay a diagnosis. The Langfuse adapter runs against a fake SDK client: no network.
"""

import dataclasses
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import structlog

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.memory import FakeLLM, InMemoryUsageLedger
from cicd_doctor.adapters.memory.llm import ScriptItem, malformed, timeout, valid
from cicd_doctor.adapters.observability import NoopTracer, RecordingTracer
from cicd_doctor.adapters.observability.langfuse_tracer import (
    MAX_TEXT_CHARS,
    LangfuseTracer,
    mask,
)
from cicd_doctor.app.container import make_tracer
from cicd_doctor.app.settings import Settings
from cicd_doctor.core.analysis import JobAnalysis
from cicd_doctor.core.diagnosis import pipeline
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext
from cicd_doctor.core.models import (
    CachedDiagnosis,
    Category,
    LLMTrace,
    LogLine,
    SimilarFailure,
)
from cicd_doctor.core.ports import Tracer
from tests.factories import T0
from tests.llmcases import analysis_for
from tests.redaction_corpus import planted_secrets

PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
CONTEXT = DiagnosisContext(installation_id=7, is_fork=False)


@pytest.fixture(scope="module")
def analysis() -> JobAnalysis:
    return analysis_for("py-modnf")


def make_job(script: list[ScriptItem], tracer: Tracer | None, ledger: Any = None) -> DiagnoseJob:
    async def no_sleep(delay: float) -> None:
        return None

    return DiagnoseJob(
        llm=FakeLLM(script),
        ledger=ledger or InMemoryUsageLedger(FakeClock(T0)),
        cost=PRICES,
        max_output_tokens=800,
        sleep=no_sleep,
        tracer=tracer,
    )


# --- the pipeline: one trace per real call ----------------------------------------------------


async def test_a_call_gives_one_trace_with_tokens_cost_latency_and_prompt_version(
    analysis: JobAnalysis,
) -> None:
    tracer = RecordingTracer()

    result = await make_job([valid()], tracer).run(analysis, CONTEXT)

    [trace] = tracer.traces
    assert result.published.source == "llm"
    assert (trace.model, trace.prompt_version, trace.attempt) == ("fake-model", "diag-v3", 1)
    assert trace.outcome == "ok"
    assert (trace.input_tokens, trace.output_tokens, trace.latency_ms) == (2000, 300, 40)
    assert trace.cost_usd == Decimal("0.001280")
    assert trace.installation_id == 7
    assert trace.output_text is not None
    assert "category" in trace.output_text
    assert trace.input_text.startswith("excerpt sha256:")


async def test_a_repair_is_its_own_trace(analysis: JobAnalysis) -> None:
    tracer = RecordingTracer()

    await make_job([malformed(), valid()], tracer).run(analysis, CONTEXT)

    first, second = tracer.traces
    assert (first.attempt, first.outcome, second.attempt, second.outcome) == (
        1,
        "invalid",
        2,
        "repaired",
    )
    assert first.error  # why it was invalid
    assert "repair_error:" in second.input_text


async def test_a_failed_call_is_one_error_trace_without_tokens(analysis: JobAnalysis) -> None:
    tracer = RecordingTracer()

    result = await make_job([timeout(), timeout(), timeout()], tracer).run(analysis, CONTEXT)

    [trace] = tracer.traces  # the three tries of one call are one call
    assert result.published.fallback_reason == "llm_error"
    assert (trace.outcome, trace.model, trace.error) == ("error", None, "LLMTransientError")
    assert (trace.input_tokens, trace.output_tokens, trace.cost_usd) == (0, 0, Decimal(0))


async def test_no_call_means_no_trace(analysis: JobAnalysis) -> None:
    origin = UUID("00000000-0000-4000-8000-0000000000aa")
    cached = CachedDiagnosis(
        diagnosis=(await make_job([valid()], None).run(analysis, CONTEXT)).published,
        origin_id=origin,
    )
    tracer = RecordingTracer()
    broke = InMemoryUsageLedger(FakeClock(T0), daily_budget_usd=Decimal(0))

    hit = await make_job([], tracer).run(
        analysis, DiagnosisContext(installation_id=7, is_fork=False, cached=cached)
    )
    blocked = await make_job([], tracer, broke).run(analysis, CONTEXT)
    fork = await make_job([], tracer).run(analysis, DiagnosisContext(7, is_fork=True))
    off = await make_job([], tracer).run(analysis, DiagnosisContext(7, False, llm_enabled=False))

    assert hit.published.source == "cache"
    assert blocked.published.fallback_reason == "budget"
    assert (fork.published.fallback_reason, off.published.fallback_reason) == (
        "fork_policy",
        "disabled",
    )
    assert tracer.traces == []


class ExplodingTracer:
    def record(self, trace: LLMTrace) -> None:
        raise RuntimeError("langfuse is down")

    async def aclose(self, timeout_s: float) -> None:
        raise RuntimeError("langfuse is down")


async def test_a_tracer_that_raises_does_not_touch_the_diagnosis(analysis: JobAnalysis) -> None:
    broken = await make_job([valid()], ExplodingTracer()).run(analysis, CONTEXT)
    plain = await make_job([valid()], None).run(analysis, CONTEXT)

    assert broken.published == plain.published
    assert broken.usage == plain.usage


async def test_a_failing_tracer_is_warned_about_once_per_process(
    analysis: JobAnalysis, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "_tracer_failure_logged", False)
    caplog.set_level(logging.WARNING)

    for _ in range(3):
        await make_job([valid()], ExplodingTracer()).run(analysis, CONTEXT)

    warnings = [r.getMessage() for r in caplog.records if "the tracer failed" in r.getMessage()]
    assert warnings == ["the tracer failed (record)"]


# --- the Langfuse adapter, over a fake SDK client ---------------------------------------------


class FakeObservation:
    def __init__(self, sdk: "FakeLangfuse") -> None:
        self.sdk = sdk
        self.end_time: int | None = None

    def end(self, *, end_time: int | None = None) -> None:
        self.end_time = end_time
        self.sdk.ended += 1


class FakeLangfuse:
    """Records every payload handed to it, like the SDK would send them."""

    def __init__(self, *, block: threading.Event | None = None, fail: bool = False) -> None:
        self.observations: list[dict[str, Any]] = []
        self.propagated: list[dict[str, Any]] = []
        self.ended = 0
        self.flushed = self.shut_down = False
        self.block = block
        self.fail = fail

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        if self.block is not None:
            self.block.wait(30)
        if self.fail:
            raise ConnectionError("down")
        self.observations.append(kwargs)
        return FakeObservation(self)

    def propagate(self, **kwargs: Any) -> Any:
        self.propagated.append(kwargs)
        return _null()

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.shut_down = True

    def everything(self) -> str:
        return json.dumps([self.observations, self.propagated], default=str)


@contextmanager
def _null() -> Iterator[None]:
    yield


def langfuse_tracer(sdk: FakeLangfuse) -> LangfuseTracer:
    return LangfuseTracer(sdk, propagate=sdk.propagate)


async def test_the_adapter_sends_one_generation_per_trace(analysis: JobAnalysis) -> None:
    sdk = FakeLangfuse()
    tracer = langfuse_tracer(sdk)
    structlog.contextvars.bind_contextvars(delivery_id="d-1", run_id=99, repository="o/r")
    try:
        await make_job([malformed(), valid()], tracer).run(analysis, CONTEXT)
    finally:
        structlog.contextvars.clear_contextvars()
    await tracer.aclose(2)

    first, second = sdk.observations
    assert first["as_type"] == "generation"
    assert (first["name"], second["name"]) == ("diagnose", "diagnose-repair")
    assert first["model"] == "fake-model"
    assert first["version"] == "diag-v3"
    assert first["usage_details"] == {"input": 2000, "output": 300, "total": 2300}
    assert first["cost_details"] == {"total": pytest.approx(0.00128)}
    assert first["level"] == "WARNING"
    assert "level" not in second
    meta = second["metadata"]
    assert (meta["delivery_id"], meta["run_id"], meta["repository"]) == ("d-1", "99", "o/r")
    assert (meta["latency_ms"], meta["outcome"], meta["installation_id"]) == ("40", "repaired", "7")
    assert sdk.propagated[0]["session_id"] == "d-1"
    assert {"repair", "diag-v3"} <= set(sdk.propagated[1]["tags"])
    assert sdk.ended == 2
    assert (sdk.flushed, sdk.shut_down) == (True, True)


async def test_a_hanging_sdk_never_blocks_a_diagnosis_or_the_shutdown(
    analysis: JobAnalysis,
) -> None:
    release = threading.Event()
    sdk = FakeLangfuse(block=release)
    tracer = langfuse_tracer(sdk)

    started = time.monotonic()
    result = await make_job([valid()], tracer).run(analysis, CONTEXT)
    await tracer.aclose(0.3)
    elapsed = time.monotonic() - started
    release.set()

    assert result.published.source == "llm"
    assert elapsed < 2


async def test_a_failing_sdk_warns_once_per_kind_and_the_diagnosis_goes_on(
    analysis: JobAnalysis, caplog: pytest.LogCaptureFixture
) -> None:
    sdk = FakeLangfuse(fail=True)
    tracer = langfuse_tracer(sdk)

    results = [await make_job([valid()], tracer).run(analysis, CONTEXT) for _ in range(3)]
    await tracer.aclose(2)

    assert all(r.published.source == "llm" for r in results)
    warnings = [r.getMessage() for r in caplog.records if "tracing failed" in r.getMessage()]
    assert warnings == ["tracing failed (ConnectionError); traces may be lost"]


async def test_a_full_queue_drops_traces_instead_of_blocking(analysis: JobAnalysis) -> None:
    release = threading.Event()
    sdk = FakeLangfuse(block=release)
    tracer = langfuse_tracer(sdk)
    trace = LLMTrace(
        model="m", prompt_version="diag-v3", attempt=1, outcome="ok", input_text="x",
        installation_id=1,
    )  # fmt: skip

    started = time.monotonic()
    for _ in range(1000):
        tracer.record(trace)
    elapsed = time.monotonic() - started
    release.set()
    await tracer.aclose(2)

    assert elapsed < 1


# --- no secret leaves the process -------------------------------------------------------------


async def test_a_planted_secret_reaches_no_payload(analysis: JobAnalysis) -> None:
    """The excerpt, a similar failure and the model's answer each carry a planted fake secret, as
    if every upstream redaction had missed it: the adapter still sends none of them."""
    secrets = planted_secrets()
    in_log, in_similar, in_output = secrets[0], secrets[3], secrets[6]
    excerpt = analysis.excerpt.model_copy(
        update={
            "lines": [LogLine(number=1, text=in_log.line), *analysis.excerpt.lines],
        }
    )
    poisoned = dataclasses.replace(analysis, excerpt=excerpt)
    similar = SimilarFailure(
        failure_id=UUID("00000000-0000-4000-8000-0000000000bb"),
        run_id=1,
        run_attempt=1,
        score=0.9,
        category=Category.DEPENDENCY,
        probable_cause=in_similar.line,
        fix_commit_sha=None,
    )
    sdk = FakeLangfuse()
    tracer = langfuse_tracer(sdk)
    context = DiagnosisContext(installation_id=7, is_fork=False, similar=[similar])
    job = make_job([valid(probable_cause=f"it leaked {in_output.line}")], tracer)
    structlog.contextvars.bind_contextvars(repository=in_log.line)  # even a logging field
    try:
        await job.run(poisoned, context)
    finally:
        structlog.contextvars.clear_contextvars()
    await tracer.aclose(2)

    sent = sdk.everything()
    assert sdk.observations  # the trace was sent, so the assertions below mean something
    for planted in (in_log, in_similar, in_output):
        assert planted.secret not in sent, planted.kind
    assert "[REDACTED:" in sent


def test_every_field_of_a_trace_is_redacted_at_the_boundary() -> None:
    secrets = planted_secrets()
    sdk = FakeLangfuse()
    tracer = langfuse_tracer(sdk)
    trace = LLMTrace(
        model=secrets[1].line,
        prompt_version=secrets[2].line,
        attempt=1,
        outcome="error",
        input_text=secrets[3].line,
        output_text=secrets[4].line,
        installation_id=1,
        error=secrets[5].line,
        metadata={secrets[7].line: secrets[6].line},
    )

    tracer.record(trace)
    tracer._queue.put(None)
    tracer._thread.join(2)

    sent = sdk.everything()
    assert sdk.observations
    for planted in secrets[1:8]:
        assert planted.secret not in sent, planted.kind


def test_the_sdk_mask_redacts_nested_strings() -> None:
    secret = planted_secrets()[0]

    masked = mask(data={"a": [secret.line, {"b": secret.line}], "n": 3})

    assert secret.secret not in json.dumps(masked)
    assert masked["n"] == 3


def test_text_is_cut_after_redaction() -> None:
    sdk = FakeLangfuse()
    tracer = langfuse_tracer(sdk)
    trace = LLMTrace(
        model="m", prompt_version="diag-v3", attempt=1, outcome="ok", installation_id=1,
        input_text="a line of a log\n" * 2000,
    )  # fmt: skip

    tracer.record(trace)
    tracer._queue.put(None)
    tracer._thread.join(2)

    assert len(sdk.observations[0]["input"]) <= MAX_TEXT_CHARS + 10


# --- the default ------------------------------------------------------------------------------


def test_without_langfuse_settings_the_tracer_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5433/d")
    for name in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    for host, public, secret in [(None, None, None), ("https://x", "pk", None), (None, "pk", "sk")]:
        settings = Settings(
            _env_file=None,
            langfuse_host=host,
            langfuse_public_key=public,
            langfuse_secret_key=secret,  # type: ignore[arg-type]
        )
        assert isinstance(make_tracer(settings), NoopTracer)


async def test_the_noop_tracer_changes_nothing(analysis: JobAnalysis) -> None:
    with_noop = await make_job([valid()], NoopTracer()).run(analysis, CONTEXT)
    without = await make_job([valid()], None).run(analysis, CONTEXT)

    assert with_noop == without
