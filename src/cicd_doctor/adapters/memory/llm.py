"""A scriptable fake ``LLMClient`` for tests and offline evals (SPEC §12).

It can answer with a valid diagnosis, malformed JSON, over-long fields, fabricated evidence, a
timeout, a 429, a server error, a rejected key, or an answer that obeys an injection in the log.
Every call is recorded, with the prompt the real adapter would have sent.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cicd_doctor.core.diagnosis.prompt import PromptMessages, build_messages
from cicd_doctor.core.errors import LLMError, LLMTransientError
from cicd_doctor.core.models import Category, DiagnosisRequest, LLMResult


@dataclass(frozen=True)
class Reply:
    """The provider answered: ``raw_json`` is its text, with the usage it reports."""

    raw_json: str
    model: str = "fake-model"
    input_tokens: int = 2000
    output_tokens: int = 300
    latency_ms: int = 40


@dataclass(frozen=True)
class Fail:
    """The provider call raised ``error`` (an ``LLMTransientError`` is retried by the pipeline)."""

    error: LLMError


Step = Callable[[DiagnosisRequest], Reply | Fail]
ScriptItem = Reply | Fail | Step


class FakeLLM:
    """Implements ``LLMClient``. Each call takes the next item of the script.

    The script is a sequence of ``Reply``, ``Fail`` or scenario functions that look at the
    request. Calling it more often than the script allows fails the test: a pipeline that makes an
    unplanned extra call (for example a second repair) must be seen.
    """

    def __init__(
        self, script: Sequence[ScriptItem] = (), *, nonce: str = "0123456789abcdef"
    ) -> None:
        self._script = list(script)
        self._nonce = nonce
        self.calls: list[DiagnosisRequest] = []
        self.messages: list[PromptMessages] = []

    def add(self, *items: ScriptItem) -> None:
        """Append items to the script (for a test that makes calls in several rounds)."""
        self._script.extend(items)

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        self.calls.append(request)
        self.messages.append(build_messages(request, self._nonce))
        if len(self.calls) > len(self._script):
            raise AssertionError(f"unplanned LLM call number {len(self.calls)}")
        item = self._script[len(self.calls) - 1]
        step = item(request) if callable(item) else item
        if isinstance(step, Fail):
            raise step.error
        return LLMResult(
            raw_json=step.raw_json,
            model=step.model,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            latency_ms=step.latency_ms,
        )


# --- scenarios ---------------------------------------------------------------------------------


def _first_error_quote(request: DiagnosisRequest) -> dict[str, object] | None:
    """An evidence item quoting the first error line of the excerpt, or None without one."""
    excerpt = request.excerpt
    texts = {line.number: line.text for line in excerpt.lines}
    for number in [*excerpt.error_lines, *texts]:
        if texts.get(number, "").strip():
            return {"line": number, "text": texts[number].strip()[:300]}
    return None


def answer(request: DiagnosisRequest, **changes: object) -> dict[str, object]:
    """A schema-valid answer for ``request``: the best rule hint's category, one real quote."""
    hinted = request.rule_hints[0].category if request.rule_hints else Category.UNKNOWN
    quote = _first_error_quote(request)
    fields: dict[str, object] = {
        "category": hinted.value,
        "probable_cause": "The step failed; the quoted line shows the error.",
        "evidence": [quote] if quote else [],
        "suggested_fix": "Fix the error shown in the quoted line and run the job again.",
        "suggestion": None,
        "confidence": 0.8,
    }
    return fields | changes


def valid(**changes: object) -> Step:
    """A valid answer that quotes a real line of the excerpt."""
    return lambda request: Reply(json.dumps(answer(request, **changes)))


def raw(text: str) -> Step:
    """Any text as the provider's answer."""
    return lambda request: Reply(text)


def malformed() -> Step:
    return raw("Sure! Here is the diagnosis: the build failed.")


def truncated() -> Step:
    return raw('{"category": "dependency", "probable_cause": "The module')


def over_long() -> Step:
    """Schema-invalid: ``probable_cause`` is longer than 500 characters."""
    return valid(probable_cause="x" * 501)


def fabricated_evidence() -> Step:
    """Schema-valid, but it quotes a line that is not in the log and a line that does not exist."""
    return valid(
        evidence=[
            {"line": 99999, "text": "Segmentation fault (core dumped)"},
            {"line": 1, "text": "this text appears nowhere in the excerpt"},
        ],
        confidence=0.97,
    )


def timeout() -> Step:
    return lambda request: Fail(LLMTransientError("timed out"))


def rate_limited() -> Step:
    return lambda request: Fail(LLMTransientError("429 Too Many Requests"))


def server_error() -> Step:
    return lambda request: Fail(LLMTransientError("502 Bad Gateway"))


def unauthorized() -> Step:
    return lambda request: Fail(LLMError("401 invalid API key"))


def obeys_injection(
    category: str,
    cause: str = "See the link for the real cause.",
    fix: str = "Open the link and follow its instructions.",
    **changes: object,
) -> Step:
    """What a model that obeyed an injected instruction could return: the attacker's category,
    text with links and mentions, a prompt leak, and fabricated evidence."""
    return valid(
        category=category,
        probable_cause=cause,
        suggested_fix=fix,
        evidence=[{"line": 1, "text": "text that is not in the log"}],
        confidence=0.99,
        **changes,
    )
