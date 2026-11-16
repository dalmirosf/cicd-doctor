"""The LLM output contract (SPEC §5.1): the schema, strict parsing and the repair message."""

import json
from typing import Any

import pytest

from cicd_doctor.core.diagnosis.schema import LLMDiagnosisOutput, strict_output_schema
from cicd_doctor.core.diagnosis.validate import InvalidOutputError, parse_output
from cicd_doctor.core.models import Category
from tests.llmcases import answer, raw

DROPPED = {"title", "description", "default", "minLength", "maxLength", "minItems", "maxItems"}
DROPPED |= {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "format"}


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found += _objects(value)
    elif isinstance(node, list):
        for item in node:
            found += _objects(item)
    return found


def _keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    keys |= _keys(sub)
            else:
                keys.add(key)
                keys |= _keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _keys(item)
    return keys


def test_the_strict_schema_has_the_shape_of_the_spec() -> None:
    schema = strict_output_schema()

    assert set(schema["properties"]) == {
        "category",
        "probable_cause",
        "evidence",
        "suggested_fix",
        "suggestion",
        "confidence",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["$defs"]["Category"]["enum"] == [category.value for category in Category]
    evidence = schema["$defs"]["LLMEvidence"]
    assert set(evidence["required"]) == {"line", "text"}
    assert {"type": "null"} in schema["properties"]["suggestion"]["anyOf"]


def test_every_object_is_closed_and_requires_all_its_properties() -> None:
    objects = _objects(strict_output_schema())

    assert len(objects) == 3  # the answer, an evidence item and a suggestion
    for node in objects:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_the_strict_schema_leaves_out_the_limits_the_provider_may_reject() -> None:
    assert _keys(strict_output_schema()) & DROPPED == set()


def test_the_limits_stay_in_the_pydantic_model() -> None:
    full = LLMDiagnosisOutput.model_json_schema()

    assert _keys(full) & {"maxLength", "maxItems", "minimum", "maximum"}


def test_a_valid_answer_parses() -> None:
    output = parse_output(raw())

    assert output.category is Category.DEPENDENCY
    assert output.evidence[0].line == 5
    assert output.suggestion is None
    assert output.confidence == 0.9


def test_a_suggestion_parses() -> None:
    suggestion = {"path": "pyproject.toml", "start_line": 3, "end_line": 4, "replacement": "x"}

    assert parse_output(raw(suggestion=suggestion)).suggestion is not None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Invalid JSON"),
        ("not json at all", "Invalid JSON"),
        ('{"category": "dependency"', "Invalid JSON"),
        ("[]", "(root)"),
        (json.dumps({k: v for k, v in answer().items() if k != "confidence"}), "confidence"),
        (json.dumps(answer() | {"unexpected": 1}), "unexpected"),
        (raw(category="security"), "category"),
        (raw(category="Dependency"), "category"),
        (raw(probable_cause="x" * 501), "probable_cause"),
        (raw(suggested_fix="x" * 1001), "suggested_fix"),
        (raw(evidence=[{"line": 5, "text": "x" * 301}]), "evidence.0.text"),
        (raw(evidence=[{"line": 5, "text": "x"}] * 9), "evidence"),
        (raw(evidence=[{"line": "5", "text": "x"}]), "evidence.0.line"),
        (raw(evidence=[{"line": 5}]), "evidence.0.text"),
        (raw(confidence=1.5), "confidence"),
        (raw(confidence=-0.1), "confidence"),
        (raw(confidence="high"), "confidence"),
        (raw(suggestion={"path": "a", "start_line": 1}), "suggestion"),
        (
            raw(
                suggestion={"path": "a", "start_line": 1, "end_line": 1, "replacement": "x" * 2001}
            ),
            "replacement",
        ),
        (raw(probable_cause=None), "probable_cause"),
    ],
)
def test_an_answer_that_is_not_the_schema_is_rejected(text: str, message: str) -> None:
    with pytest.raises(InvalidOutputError) as raised:
        parse_output(text)

    assert message in str(raised.value)


def test_the_repair_message_never_echoes_the_answer() -> None:
    secret_looking = "SECRET-INSTRUCTIONS-FROM-THE-LOG-do-this-now"
    text = raw(category=secret_looking, probable_cause="y" * 600)

    with pytest.raises(InvalidOutputError) as raised:
        parse_output(text)

    message = str(raised.value)
    assert secret_looking not in message
    assert "y" * 50 not in message
    assert "category" in message
    assert "probable_cause" in message


def test_a_long_list_of_problems_is_capped() -> None:
    bad = raw(evidence=[{"line": "x", "text": 1}] * 8, confidence="a", category="b")

    with pytest.raises(InvalidOutputError) as raised:
        parse_output(bad)

    assert len(str(raised.value)) <= 600
