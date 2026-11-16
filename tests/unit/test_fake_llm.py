"""The scriptable fake LLM (SPEC §12): its scenarios must do exactly what their names say."""

import json

import pytest

from cicd_doctor.adapters.memory import FakeLLM
from cicd_doctor.adapters.memory.llm import (
    Fail,
    Reply,
    answer,
    fabricated_evidence,
    malformed,
    obeys_injection,
    over_long,
    rate_limited,
    raw,
    server_error,
    timeout,
    truncated,
    unauthorized,
    valid,
)
from cicd_doctor.core.diagnosis.validate import InvalidOutputError, finalize, parse_output
from cicd_doctor.core.errors import LLMError, LLMTransientError
from cicd_doctor.core.models import Category, DiagnosisRequest
from tests.llmcases import analysis_for, make_excerpt, make_hint, make_request


def _requests() -> list[DiagnosisRequest]:
    analysis = analysis_for("py-modnf")
    unmatched = analysis_for("no-step-data")
    return [
        make_request(),
        make_request(rule_hints=[make_hint()]),
        make_request(make_excerpt([])),
        make_request(make_excerpt(["only a blank-ish line", "   "], error_lines=[])),
        DiagnosisRequest(job=analysis.job, excerpt=analysis.excerpt, rule_hints=analysis.matches),
        DiagnosisRequest(job=unmatched.job, excerpt=unmatched.excerpt),
    ]


async def test_calls_follow_the_script_in_order_and_are_recorded() -> None:
    fake = FakeLLM([Reply('{"a": 1}', model="m-1", input_tokens=5, output_tokens=6), raw("second")])
    request = make_request()

    first = await fake.diagnose(request)
    second = await fake.diagnose(request)

    assert (first.raw_json, first.model, first.input_tokens, first.output_tokens) == (
        '{"a": 1}',
        "m-1",
        5,
        6,
    )
    assert second.raw_json == "second"
    assert fake.calls == [request, request]
    assert len(fake.messages) == 2
    assert fake.messages[0].nonce == "0123456789abcdef"
    assert "Trusted context" in fake.messages[0].user  # the prompt the real adapter would send


async def test_an_unplanned_call_fails_the_test() -> None:
    fake = FakeLLM([raw("only one")])
    await fake.diagnose(make_request())

    with pytest.raises(AssertionError, match="unplanned LLM call number 2"):
        await fake.diagnose(make_request())


async def test_the_script_can_be_extended() -> None:
    fake = FakeLLM()
    fake.add(raw("late"))

    assert (await fake.diagnose(make_request())).raw_json == "late"


@pytest.mark.parametrize("request_", _requests())
async def test_valid_answers_follow_the_schema_and_quote_the_excerpt(
    request_: DiagnosisRequest,
) -> None:
    result = await FakeLLM([valid()]).diagnose(request_)

    output = parse_output(result.raw_json)
    diagnosis = finalize(output, request_.excerpt, model=result.model, prompt_version="diag-v1")
    expected = request_.rule_hints[0].category if request_.rule_hints else Category.UNKNOWN
    assert diagnosis.category is expected
    has_text = any(line.text.strip() for line in request_.excerpt.lines)
    assert diagnosis.evidence_verified is has_text  # it quotes a real line when there is one


async def test_valid_accepts_overrides() -> None:
    result = await FakeLLM([valid(category="network", confidence=0.4)]).diagnose(make_request())

    output = parse_output(result.raw_json)
    assert (output.category, output.confidence) == (Category.NETWORK, 0.4)


@pytest.mark.parametrize("scenario", [malformed(), truncated(), over_long(), raw("")])
async def test_invalid_scenarios_are_rejected_by_the_parser(scenario: object) -> None:
    result = await FakeLLM([scenario]).diagnose(make_request())  # type: ignore[list-item]

    with pytest.raises(InvalidOutputError):
        parse_output(result.raw_json)


async def test_fabricated_evidence_is_valid_json_with_nothing_to_stand_on() -> None:
    request = make_request()
    result = await FakeLLM([fabricated_evidence()]).diagnose(request)

    output = parse_output(result.raw_json)
    diagnosis = finalize(output, request.excerpt, model="m", prompt_version="diag-v1")
    assert output.confidence == 0.97
    assert (diagnosis.evidence, diagnosis.evidence_verified, diagnosis.confidence) == (
        [],
        False,
        0.3,
    )


@pytest.mark.parametrize(
    ("scenario", "transient"),
    [
        (timeout(), True),
        (rate_limited(), True),
        (server_error(), True),
        (unauthorized(), False),
        (Fail(LLMError("x")), False),
        (Fail(LLMTransientError("x")), True),
    ],
)
async def test_failures_raise_the_right_kind_of_error(scenario: object, transient: bool) -> None:
    fake = FakeLLM([scenario])  # type: ignore[list-item]

    with pytest.raises(LLMError) as raised:
        await fake.diagnose(make_request())

    assert isinstance(raised.value, LLMTransientError) is transient


async def test_an_obedient_answer_is_schema_valid_but_wrong_and_unsupported() -> None:
    request = make_request()
    step = obeys_injection("config_or_secrets", cause="Visit https://evil.example/fix @octocat")
    result = await FakeLLM([step]).diagnose(request)

    output = parse_output(result.raw_json)
    diagnosis = finalize(output, request.excerpt, model="m", prompt_version="diag-v1")
    assert diagnosis.category is Category.CONFIG_OR_SECRETS  # the attacker's choice
    assert "https://evil.example/fix" in diagnosis.probable_cause  # text is not filtered here
    assert diagnosis.evidence_verified is False  # but nothing in the log supports it
    assert diagnosis.confidence <= 0.3


def test_answer_builds_the_fields_of_the_schema() -> None:
    fields = answer(make_request(rule_hints=[make_hint()]), confidence=0.1)

    assert set(fields) == {
        "category",
        "probable_cause",
        "evidence",
        "suggested_fix",
        "suggestion",
        "confidence",
    }
    assert fields["confidence"] == 0.1
    assert json.loads(json.dumps(fields)) == fields
