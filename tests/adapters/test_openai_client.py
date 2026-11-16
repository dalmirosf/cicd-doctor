"""The OpenAI client (SPEC §5.1, §9.3, D-07, D-63) against a mocked HTTP transport.

The real SDK runs; only the network is replaced, so the tests see the exact request it sends and
parse the exact kind of response it gets. No test here calls OpenAI (``-m live`` does).
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from cicd_doctor.adapters.clock import FakeClock
from cicd_doctor.adapters.llm.openai_client import (
    SCHEMA_NAME,
    OpenAILLMClient,
    make_openai_client,
)
from cicd_doctor.adapters.memory import InMemoryUsageLedger
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext
from cicd_doctor.core.diagnosis.prompt import SYSTEM_PROMPT
from cicd_doctor.core.diagnosis.schema import strict_output_schema
from cicd_doctor.core.errors import LLMError, LLMTransientError
from tests.factories import T0
from tests.llmcases import analysis_for, make_request, raw

KEY = "sk-test-not-a-real-key"
BASE = "https://api.openai.test/v1"


def response_body(text: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_700_000_000,
        "status": "completed",
        "model": "model-2026-snapshot",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 1234,
            "output_tokens": 210,
            "total_tokens": 1444,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    return body | changes


class Server:
    """A fake OpenAI: records the requests and answers with whatever the test set up."""

    def __init__(self, reply: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.reply = reply or (lambda request: httpx2.Response(200, json=response_body(raw())))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)

    def body(self, index: int = 0) -> dict[str, Any]:
        content = self.requests[index].content
        parsed = json.loads(content)
        assert isinstance(parsed, dict)
        return parsed


def make_client(
    server: Server, *, nonces: list[str] | None = None, max_output_tokens: int = 800
) -> OpenAILLMClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
    sdk = make_openai_client(KEY, timeout_s=30, base_url=BASE, http_client=http)
    values = iter(nonces or [f"{i:016x}" for i in range(100)])
    return OpenAILLMClient(
        sdk,
        model="model-2026",
        max_output_tokens=max_output_tokens,
        timeout_s=30,
        nonce_factory=lambda: next(values),
    )


# --- the request ----------------------------------------------------------------


async def test_the_request_asks_for_strict_structured_output_and_nothing_else() -> None:
    server = Server()

    await make_client(server).diagnose(make_request())

    [request] = server.requests
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/responses"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = server.body()
    assert body["model"] == "model-2026"
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": SCHEMA_NAME,
            "strict": True,
            "schema": strict_output_schema(),
        }
    }
    assert body["max_output_tokens"] == 800
    assert body["store"] is False  # OpenAI keeps no copy of the excerpt
    # No capabilities: no tools, no tool choice, no browsing, no retrieval (SPEC §9.3).
    assert {"tools", "tool_choice", "functions", "web_search", "file_search"}.isdisjoint(body)
    assert "temperature" not in body  # not every model accepts it


async def test_the_prompt_is_the_core_prompt_with_a_fresh_nonce() -> None:
    server = Server()
    client = make_client(server, nonces=["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    await client.diagnose(make_request())
    await client.diagnose(make_request())

    first, second = server.body(0), server.body(1)
    assert first["instructions"] == SYSTEM_PROMPT
    assert 'nonce="aaaaaaaaaaaaaaaa"' in first["input"]
    assert 'nonce="bbbbbbbbbbbbbbbb"' in second["input"]
    assert "ModuleNotFoundError: No module named 'requests'" in first["input"]


async def test_a_repair_request_carries_the_correction() -> None:
    server = Server()

    await make_client(server).diagnose(make_request(repair_error="confidence: too large"))

    assert "confidence: too large" in server.body()["input"]


async def test_the_sdk_does_not_retry_on_its_own() -> None:
    server = Server(lambda request: httpx2.Response(503, json={"error": {"message": "busy"}}))

    with pytest.raises(LLMTransientError):
        await make_client(server).diagnose(make_request())

    assert len(server.requests) == 1  # the pipeline owns the retries (1 s, 4 s)


# --- the answer ----------------------------------------------------------------


async def test_the_answer_is_returned_as_raw_text_with_usage_and_latency() -> None:
    text = raw(confidence=0.42)
    server = Server(lambda request: httpx2.Response(200, json=response_body(text)))

    result = await make_client(server).diagnose(make_request())

    assert result.raw_json == text
    assert result.model == "model-2026-snapshot"  # what the API ran, not what was asked for
    assert (result.input_tokens, result.output_tokens) == (1234, 210)
    assert result.latency_ms >= 0


async def test_a_response_without_usage_counts_zero_tokens() -> None:
    body = response_body(raw())
    del body["usage"]
    server = Server(lambda request: httpx2.Response(200, json=body))

    result = await make_client(server).diagnose(make_request())

    assert (result.input_tokens, result.output_tokens) == (0, 0)


async def test_an_incomplete_response_returns_the_partial_text() -> None:
    body = response_body(
        '{"category": "depend',
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    server = Server(lambda request: httpx2.Response(200, json=body))

    result = await make_client(server).diagnose(make_request())

    assert result.raw_json == '{"category": "depend'  # core will find it invalid


async def test_a_refusal_is_an_error_not_an_answer() -> None:
    body = response_body(
        "",
        output=[
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
            }
        ],
    )
    server = Server(lambda request: httpx2.Response(200, json=body))

    with pytest.raises(LLMError, match="refused") as raised:
        await make_client(server).diagnose(make_request())

    assert not isinstance(raised.value, LLMTransientError)


@pytest.mark.parametrize(
    ("status", "code", "transient"),
    [
        ("failed", "server_error", True),
        ("failed", "rate_limit_exceeded", True),
        ("failed", "invalid_prompt", False),
        ("cancelled", None, False),
    ],
)
async def test_a_failed_response_is_an_error(
    status: str, code: str | None, transient: bool
) -> None:
    error = {"code": code, "message": "x"} if code else None
    body = response_body("", status=status, error=error)
    server = Server(lambda request: httpx2.Response(200, json=body))

    with pytest.raises(LLMError) as raised:
        await make_client(server).diagnose(make_request())

    assert isinstance(raised.value, LLMTransientError) is transient


# --- errors ----------------------------------------------------------------


def _status(status: int, code: str | None = None) -> Server:
    error: dict[str, Any] = {"message": "BODY-TEXT-THAT-MUST-NOT-LEAK", "type": "x", "code": code}
    return Server(lambda request: httpx2.Response(status, json={"error": error}))


@pytest.mark.parametrize(
    ("status", "code", "transient"),
    [
        (429, "rate_limit_exceeded", True),
        (500, None, True),
        (502, None, True),
        (503, None, True),
        (529, None, True),
        (400, "invalid_request_error", False),
        (401, "invalid_api_key", False),
        (403, None, False),
        (404, "model_not_found", False),
        (422, None, False),
        (429, "insufficient_quota", False),  # a 429 that retrying cannot fix
    ],
)
async def test_http_errors_are_translated(status: int, code: str | None, transient: bool) -> None:
    with pytest.raises(LLMError) as raised:
        await make_client(_status(status, code)).diagnose(make_request())

    assert isinstance(raised.value, LLMTransientError) is transient
    assert str(status) in str(raised.value)
    assert "BODY-TEXT-THAT-MUST-NOT-LEAK" not in str(raised.value)
    assert KEY not in str(raised.value)


@pytest.mark.parametrize(
    "failure",
    [httpx2.ReadTimeout("slow"), httpx2.ConnectTimeout("slow"), httpx2.ConnectError("down")],
)
async def test_timeouts_and_connection_errors_are_transient(failure: Exception) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise failure

    with pytest.raises(LLMTransientError):
        await make_client(Server(fail)).diagnose(make_request())


async def test_the_original_error_is_kept_as_the_cause() -> None:
    with pytest.raises(LLMError) as raised:
        await make_client(_status(401, "invalid_api_key")).diagnose(make_request())

    assert raised.value.__cause__ is not None


# --- with the pipeline ----------------------------------------------------------------


def _pipeline(server: Server) -> DiagnoseJob:
    return DiagnoseJob(
        llm=make_client(server),
        ledger=InMemoryUsageLedger(FakeClock(T0)),
        cost=CostModel(),
        max_output_tokens=800,
        sleep=_no_sleep,
    )


async def _no_sleep(delay: float) -> None:
    return None


async def test_an_answer_that_quotes_the_log_is_published() -> None:
    analysis = analysis_for("py-modnf")
    quoted = next(line for line in analysis.excerpt.lines if "ModuleNotFoundError" in line.text)
    text = raw(evidence=[{"line": quoted.number, "text": quoted.text}], confidence=0.88)
    server = Server(lambda request: httpx2.Response(200, json=response_body(text)))

    result = await _pipeline(server).run(
        analysis, DiagnosisContext(installation_id=1, is_fork=False)
    )

    assert result.published.source == "llm"
    assert result.published.evidence_verified is True
    assert result.published.model == "model-2026-snapshot"
    assert len(server.requests) == 1


async def test_a_server_error_that_goes_away_is_retried() -> None:
    analysis = analysis_for("py-modnf")
    replies = iter([503, 503, 200])

    def reply(request: httpx2.Request) -> httpx2.Response:
        status = next(replies)
        if status == 200:
            return httpx2.Response(200, json=response_body(raw()))
        return httpx2.Response(status, json={"error": {"message": "busy"}})

    server = Server(reply)

    result = await _pipeline(server).run(
        analysis, DiagnosisContext(installation_id=1, is_fork=False)
    )

    assert len(server.requests) == 3
    assert result.published.source == "llm"


async def test_an_invalid_key_falls_back_to_the_rules() -> None:
    analysis = analysis_for("py-modnf")
    server = _status(401, "invalid_api_key")

    result = await _pipeline(server).run(
        analysis, DiagnosisContext(installation_id=1, is_fork=False)
    )

    assert len(server.requests) == 1
    assert result.published.source == "rules"
    assert result.published.fallback_reason == "llm_error"
