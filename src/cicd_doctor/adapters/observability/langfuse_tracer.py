"""The Langfuse tracer (D-118): one trace per model call, only redacted and capped text.

``LLMTrace`` comes from the pipeline already redacted; everything is redacted AGAIN here, at the
boundary, and the SDK gets the same redaction as its ``mask`` (defence in depth, SPEC 9.2). The
tracer never blocks and never raises into a diagnosis: ``record`` only puts the trace in a bounded
queue, and a daemon thread talks to the SDK, which itself batches and sends in the background. A
full queue drops the trace. Each kind of failure logs one WARNING, with the type name only.
"""

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast

import structlog
from langfuse import Langfuse, propagate_attributes
from pydantic import SecretStr

from cicd_doctor.core.models import LLMTrace
from cicd_doctor.core.redaction import redact

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 4000  # input and output of a trace
MAX_META_CHARS = 200  # a metadata value, a tag (the SDK's own limit for propagated values)
QUEUE_SIZE = 256
CORRELATION_KEYS = ("delivery_id", "run_id", "installation_id", "repository", "job_id", "job_kind")

Propagate = Callable[..., AbstractContextManager[Any]]


class LangfuseClient(Protocol):
    """The part of the SDK client the tracer uses (a fake in the tests)."""

    def start_observation(self, **kwargs: Any) -> Any: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


def clean(text: str, limit: int) -> str:
    """Redact the whole text, then cut it: a cut first could leave half a secret that no rule
    recognises."""
    safe = redact(text)
    return safe if len(safe) <= limit else safe[:limit] + " [cut]"


def mask(*, data: Any, **_: Any) -> Any:
    """The SDK's ``mask``: redacts every string it is about to send, whatever field it is in."""
    if isinstance(data, str):
        return clean(data, MAX_TEXT_CHARS)
    if isinstance(data, Mapping):
        return {clean(str(k), MAX_META_CHARS): mask(data=v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask(data=v) for v in data]
    return data


def make_langfuse_client(
    host: str, public_key: str, secret_key: SecretStr, *, timeout_s: int = 5
) -> LangfuseClient:
    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key.get_secret_value(),
        host=host,
        timeout=timeout_s,
        mask=mask,
    )
    return cast(LangfuseClient, client)  # the SDK's overloads are narrower than the Protocol


class LangfuseTracer:
    def __init__(
        self, client: LangfuseClient, *, propagate: Propagate = propagate_attributes
    ) -> None:
        self._client = client
        self._propagate = propagate
        self._queue: queue.Queue[tuple[LLMTrace, dict[str, str]] | None] = queue.Queue(QUEUE_SIZE)
        self._warned: set[str] = set()
        self._closed = False
        self._thread = threading.Thread(target=self._work, name="langfuse-tracer", daemon=True)
        self._thread.start()

    def record(self, trace: LLMTrace) -> None:
        """Never blocks, never raises: the trace goes to the queue with the logging context of the
        caller (``delivery_id`` and the rest of D-117), read here because the thread has none."""
        try:
            if self._closed:
                return
            context = {
                key: str(value)
                for key, value in structlog.contextvars.get_contextvars().items()
                if key in CORRELATION_KEYS
            }
            self._queue.put_nowait((trace, context))
        except queue.Full:
            self._warn("queue_full")
        except Exception as exc:
            self._warn(type(exc).__name__)

    async def aclose(self, timeout_s: float) -> None:
        """Flush and shut the SDK down, waiting at most ``timeout_s`` seconds; a hung SDK is
        abandoned (the threads are daemons)."""
        self._closed = True
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def finish() -> None:
            try:
                self._queue.put(None, timeout=timeout_s)
                self._thread.join(timeout_s)
                self._client.flush()
                self._client.shutdown()
            except Exception as exc:
                self._warn(type(exc).__name__)
            finally:
                loop.call_soon_threadsafe(done.set)

        threading.Thread(target=finish, name="langfuse-close", daemon=True).start()
        try:
            await asyncio.wait_for(done.wait(), timeout_s)
        except TimeoutError:
            self._warn("close_timeout")

    # --- the thread --------------------------------------------------------------------

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            try:
                self._send(*item)
            except Exception as exc:
                self._warn(type(exc).__name__)

    def _send(self, trace: LLMTrace, context: dict[str, str]) -> None:
        meta = {
            clean(key, 64): clean(value, MAX_META_CHARS)
            for key, value in {
                **context,
                **trace.metadata,
                "installation_id": str(trace.installation_id),
                "attempt": str(trace.attempt),
                "outcome": trace.outcome,
                "latency_ms": "" if trace.latency_ms is None else str(trace.latency_ms),
            }.items()
        }
        tags = [trace.outcome, trace.prompt_version] + (["repair"] if trace.attempt > 1 else [])
        kwargs: dict[str, Any] = {
            "name": "diagnose" if trace.attempt == 1 else "diagnose-repair",
            "as_type": "generation",
            "input": clean(trace.input_text, MAX_TEXT_CHARS),
            "metadata": meta,
            "version": clean(trace.prompt_version, MAX_META_CHARS),
            "usage_details": {
                "input": trace.input_tokens,
                "output": trace.output_tokens,
                "total": trace.input_tokens + trace.output_tokens,
            },
            "cost_details": {"total": float(trace.cost_usd)},
        }
        if trace.model:
            kwargs["model"] = clean(trace.model, MAX_META_CHARS)
        if trace.output_text is not None:
            kwargs["output"] = clean(trace.output_text, MAX_TEXT_CHARS)
        if trace.outcome in ("invalid", "error"):
            kwargs["level"] = "ERROR" if trace.outcome == "error" else "WARNING"
            kwargs["status_message"] = clean(trace.error or trace.outcome, MAX_META_CHARS)
        session = context.get("delivery_id")
        with self._propagate(
            trace_name="diagnose",
            session_id=clean(session, MAX_META_CHARS) if session else None,
            tags=[clean(tag, MAX_META_CHARS) for tag in tags],
            metadata=meta,
            version=kwargs["version"],
        ):
            observation = self._client.start_observation(**kwargs)
            # The SDK stamps the start at "now": the end is "now" plus the measured latency, so
            # the UI shows the latency of the call and not the time spent building the trace.
            end = time.time_ns() + (trace.latency_ms or 0) * 1_000_000
            observation.end(end_time=end)

    def _warn(self, kind: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            logger.warning("tracing failed (%s); traces may be lost", clean(kind, 80))
