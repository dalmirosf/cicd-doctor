"""The live smoke test of SPEC §13 P1b: five real logs, one real model.

It checks the contract, not the quality: the answer is valid JSON that respects the §5.1 schema,
it survives the validation of the pipeline (no fallback), and what is published points at lines
that are in the excerpt. How good the answers are is measured by ``python -m evals run`` (the
mini-eval that chooses ``OPENAI_MODEL``, D-65).
"""

import pytest

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from evals.runner.cases import DEFAULT_CASES_DIR, load_cases
from evals.runner.variants import run_llm_case

# One case per kind of failure the rules see (and one they cannot name).
SMOKE_CASES = [
    "lab-py-modnf-01",  # dependency, a rule matches
    "lab-py-assert-01",  # broken_test
    "lab-oom-137-01",  # resource exhaustion
    "lab-flaky-random-01",  # flaky: the log looks like a broken test
    "lab-opaque-exit-01",  # no rule matches: the model works alone
]


@pytest.mark.parametrize("case_id", SMOKE_CASES)
async def test_the_model_answers_inside_the_contract(
    case_id: str, pipeline: DiagnoseJob, counter: TiktokenCounter
) -> None:
    [case] = [c for c in load_cases(DEFAULT_CASES_DIR, ["lab"]) if c.id == case_id]

    result = await run_llm_case(
        case, counter, variant="llm+hints [live]", pipeline=pipeline, use_hints=True
    )

    assert result.error is None
    assert result.fallback_reason is None, f"fell back: {result.fallback_reason}"
    assert result.diagnosis_source == "llm"
    assert result.llm_calls in (1, 2)  # 2 only when the single repair was needed
    assert result.input_tokens > 0
    assert 0 < result.output_tokens <= 800
    assert 0.0 <= result.confidence <= 1.0
    assert set(result.evidence_lines) <= set(result.excerpt_lines)
