"""Validate and sanitize what the LLM returned (SPEC §5.1 post-validation, §9.3, §9.5, D-25).

The answer is untrusted: it was produced from untrusted logs. ``parse_output`` rejects anything
that is not the schema. ``finalize`` then makes it safe to store and show: evidence that does not
quote the excerpt is dropped (and confidence is capped when none is left), suggestions are
restricted, secrets are redacted again, and an answer that repeats the system prompt is refused.
"""

import re
from collections.abc import Sequence

from pydantic import ValidationError

from cicd_doctor.core.diagnosis.prompt import CANARY, escape_untrusted
from cicd_doctor.core.diagnosis.schema import LLMDiagnosisOutput, LLMEvidence, LLMSuggestion
from cicd_doctor.core.errors import CicdDoctorError
from cicd_doctor.core.models import Diagnosis, Evidence, LogExcerpt, PromptDiff, Suggestion
from cicd_doctor.core.redaction import redact
from cicd_doctor.core.suggestions import checked_against_diff

UNVERIFIED_CONFIDENCE_CAP = 0.3  # SPEC §5.1 rule 2
MAX_ERROR_CHARS = 600  # what is sent back to the model on the repair attempt

_PROBABLE_CAUSE_MAX = 500
_SUGGESTED_FIX_MAX = 1000
_EVIDENCE_TEXT_MAX = 300

_LINE_PREFIX = re.compile(r"^L(\d+):\s*")  # the model may copy the "L<n>: " of the prompt


class InvalidOutputError(CicdDoctorError):
    """The answer is not usable. The message says what is wrong without echoing the answer."""


def parse_output(raw_json: str) -> LLMDiagnosisOutput:
    """Strictly validate the raw answer against the schema of SPEC §5.1."""
    try:
        return LLMDiagnosisOutput.model_validate_json(raw_json, strict=True)
    except ValidationError as exc:
        raise InvalidOutputError(describe_validation_error(exc)) from None


def describe_validation_error(exc: ValidationError) -> str:
    """The failing fields and why, for the repair request: field paths and error messages only.

    Pydantic's ``input`` value is left out on purpose: it is text the model wrote after reading
    untrusted logs, and it must not travel back into the prompt.
    """
    parts = []
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"]) or "(root)"
        parts.append(f"{path}: {error['msg']}")
    return "; ".join(parts)[:MAX_ERROR_CHARS]


def finalize(
    output: LLMDiagnosisOutput,
    excerpt: LogExcerpt,
    *,
    model: str,
    prompt_version: str,
    diff: PromptDiff | None = None,
) -> Diagnosis:
    """The published form of a schema-valid answer. ``diff`` is what the model was shown."""
    texts = [output.probable_cause, output.suggested_fix]
    texts += [item.text for item in output.evidence]
    if output.suggestion is not None:
        texts += [output.suggestion.path, output.suggestion.replacement]
    if any(CANARY in text for text in texts):
        raise InvalidOutputError("the answer repeats the system prompt")

    evidence = verified_evidence(output.evidence, excerpt)
    verified = bool(evidence)
    # No verified evidence, no one-click change (D-99): the answer may have obeyed the log.
    confidence = (
        output.confidence if verified else min(output.confidence, UNVERIFIED_CONFIDENCE_CAP)
    )
    return Diagnosis(
        category=output.category,
        probable_cause=_clean(output.probable_cause, _PROBABLE_CAUSE_MAX),
        evidence=evidence,
        suggested_fix=_clean(output.suggested_fix, _SUGGESTED_FIX_MAX),
        suggestion=checked_suggestion(output.suggestion, diff) if verified else None,
        confidence=confidence,
        source="llm",
        model=model,
        prompt_version=prompt_version,
        evidence_verified=verified,
    )


def verified_evidence(items: Sequence[LLMEvidence], excerpt: LogExcerpt) -> list[Evidence]:
    """Keep the items that quote a line of the excerpt (SPEC §5.1 rule 1).

    The line number must exist in the excerpt, and the text must be a non-empty substring of that
    line once whitespace is normalised. A model that copied the ``L<n>:`` prefix of the prompt, or
    quoted the line as the prompt showed it (with the block tag escaped), still matches.
    """
    lines = {line.number: line.text for line in excerpt.lines}
    kept: list[Evidence] = []
    seen: set[tuple[int, str]] = set()
    for item in items:
        line_text = lines.get(item.line)
        if line_text is None:
            continue
        quoted = normalise_quote(item.text)
        if not quoted:
            continue
        key = (item.line, quoted)
        if key in seen or not quotes_line(quoted, line_text):
            continue
        seen.add(key)
        kept.append(Evidence(line=item.line, text=_clean(quoted, _EVIDENCE_TEXT_MAX)))
    return kept


def normalise_quote(text: str) -> str:
    """A quoted text as it is compared: without a copied ``L<n>:`` prefix, whitespace collapsed."""
    return _normalise(_LINE_PREFIX.sub("", text.strip(), count=1))


def quotes_line(quoted: str, line_text: str) -> bool:
    """Whether ``quoted`` (from ``normalise_quote``, non-empty) is a substring of the line, either
    as it is in the excerpt or as the prompt shows it (with the block tag escaped)."""
    haystacks = (_normalise(line_text), _normalise(escape_untrusted(line_text)))
    return any(quoted in haystack for haystack in haystacks)


def checked_suggestion(
    suggestion: LLMSuggestion | None, diff: PromptDiff | None
) -> Suggestion | None:
    """Keep a suggestion only if it can be applied as a change to the diff the model saw (SPEC
    §5.1 rule 3, D-97): a file of that diff, a range inside one of its shown hunks, at most 30
    lines, a clean replacement. Without a diff there is no suggestion. Otherwise it is dropped and
    ``suggested_fix`` stays as text.
    """
    if suggestion is None:
        return None
    return checked_against_diff(
        Suggestion(
            path=suggestion.path,
            start_line=suggestion.start_line,
            end_line=suggestion.end_line,
            replacement=suggestion.replacement,
        ),
        diff,
    )


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _clean(text: str, limit: int) -> str:
    """Redact secrets, then cap the length (redaction can make a text longer)."""
    return redact(text)[:limit]
