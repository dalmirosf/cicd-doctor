"""The OpenAI ``LLMClient``: Structured Outputs through the Responses API (SPEC §5.1, D-07, D-63).

One call per ``diagnose``. The request has no tools, no browsing and no retrieval (SPEC §9.3 point
2): the model gets the prompt and a strict JSON schema, nothing else. ``store`` is off, so
OpenAI keeps no copy of the excerpt. The SDK's own retries are off too: the pipeline retries
transient errors itself, after 1 s and 4 s, and must see each failure.

The answer is returned as raw text. Validating it is core's job, whatever the provider promises.
Errors are translated into ``LLMError`` (do not retry) and ``LLMTransientError`` (retry); their
messages say what failed and never carry the response body.
"""

import time
from collections.abc import Callable
from typing import Any

import openai
from openai import AsyncOpenAI

from cicd_doctor.core.diagnosis.prompt import build_messages, new_nonce
from cicd_doctor.core.diagnosis.schema import strict_output_schema
from cicd_doctor.core.errors import LLMError, LLMTransientError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult

SCHEMA_NAME = "diagnosis"
_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


def make_openai_client(
    api_key: str, *, timeout_s: float, base_url: str | None = None, http_client: Any = None
) -> AsyncOpenAI:
    """An SDK client without its own retries or default timeout policy: core owns both."""
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_s,
        max_retries=0,
        http_client=http_client,
    )


class OpenAILLMClient:
    """Implements ``LLMClient``."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        max_output_tokens: int,
        timeout_s: float,
        nonce_factory: Callable[[], str] = new_nonce,
    ) -> None:
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._timeout_s = timeout_s
        self._nonce_factory = nonce_factory

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        messages = build_messages(request, self._nonce_factory())
        started = time.perf_counter()
        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=messages.system,
                input=messages.user,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": SCHEMA_NAME,
                        "strict": True,
                        "schema": strict_output_schema(),
                    }
                },
                max_output_tokens=self._max_output_tokens,
                store=False,
                timeout=self._timeout_s,
            )
        except openai.OpenAIError as exc:
            raise translate_error(exc) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        return _result(response, self._model, latency_ms)


def translate_error(exc: openai.OpenAIError) -> LLMError:
    """Map an SDK error to the two kinds the pipeline understands. The body is never copied."""
    if isinstance(exc, openai.APITimeoutError):
        return LLMTransientError("OpenAI request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return LLMTransientError("could not connect to OpenAI")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        code = str(getattr(exc, "code", "") or "")
        label = f"OpenAI answered {status}" + (f" ({code})" if code else "")
        if code in _QUOTA_CODES:
            return LLMError(f"{label}: the account has no quota left")
        if status == 429 or status >= 500:
            return LLMTransientError(label)
        return LLMError(label)
    return LLMError(f"OpenAI client error: {type(exc).__name__}")


def _result(response: Any, requested_model: str, latency_ms: int) -> LLMResult:
    status = getattr(response, "status", None)
    if status in ("failed", "cancelled"):
        error = getattr(response, "error", None)
        code = str(getattr(error, "code", "") or "")
        message = f"OpenAI response {status}" + (f" ({code})" if code else "")
        raise (LLMTransientError if code in ("server_error", "rate_limit_exceeded") else LLMError)(
            message
        )
    if _refused(response):
        raise LLMError("the model refused to answer")
    usage = getattr(response, "usage", None)
    return LLMResult(
        raw_json=str(getattr(response, "output_text", "") or ""),  # partial when "incomplete"
        model=str(getattr(response, "model", "") or requested_model),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        latency_ms=latency_ms,
    )


def _refused(response: Any) -> bool:
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return True
    return False
