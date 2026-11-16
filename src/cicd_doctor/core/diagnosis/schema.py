"""The LLM output contract (SPEC §5.1, D-07).

``LLMDiagnosisOutput`` is what the model may return, and nothing else. The response is validated
against it in core whatever the provider promises. The provider gets ``strict_output_schema()``:
the same shape without the keywords that strict Structured Outputs may reject (D-63).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cicd_doctor.core.models import Category


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LLMEvidence(_Strict):
    line: int
    text: str = Field(max_length=300)


class LLMSuggestion(_Strict):
    path: str
    start_line: int
    end_line: int
    replacement: str = Field(max_length=2000)


class LLMDiagnosisOutput(_Strict):
    category: Category
    probable_cause: str = Field(max_length=500)
    evidence: list[LLMEvidence] = Field(max_length=8)
    suggested_fix: str = Field(max_length=1000)
    suggestion: LLMSuggestion | None  # required, null when there is none (strict mode)
    confidence: float = Field(ge=0, le=1)


# Validation keywords (and annotations) that are dropped from the schema sent to the provider.
# Strict Structured Outputs has not always accepted them; the limits are in the prompt, and
# core enforces all of them when it validates the answer.
_DROPPED_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "format",
    }
)


def strict_output_schema() -> dict[str, Any]:
    """The JSON Schema for the provider's strict mode: structure and enums, no length limits."""
    schema = LLMDiagnosisOutput.model_json_schema()
    cleaned = _clean(schema)
    assert isinstance(cleaned, dict)
    return cleaned


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROPPED_KEYWORDS:
                continue
            if key in ("properties", "$defs"):  # their keys are names, not keywords
                cleaned[key] = {name: _clean(sub) for name, sub in value.items()}
            else:
                cleaned[key] = _clean(value)
        return cleaned
    if isinstance(node, list):
        return [_clean(item) for item in node]
    return node
