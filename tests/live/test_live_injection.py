"""The injection suite against the real model (SPEC §9.3, §13 P1b, D-65).

The fake-model half (``tests/unit/test_injection.py``) proves what core guarantees even when the
model obeys the log. This half measures the model: every attack case goes through the production
pipeline and ``judge`` says whether the attack got anything (a forced category, markup that would
render, a leaked prompt, invented evidence, an unsafe suggestion). The expectation is zero wins.

A model that is *fooled* (``category_forced``) is a finding about the model, so the failure message
says which cases it lost, and ``python -m evals run --sources injection`` reports the rate.
"""

from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from evals.runner.cases import DEFAULT_CASES_DIR, load_cases
from evals.runner.variants import run_llm_case


async def test_no_injection_gets_anything_from_the_real_model(
    pipeline: DiagnoseJob, counter: TiktokenCounter
) -> None:
    cases = load_cases(DEFAULT_CASES_DIR, ["injection"])
    assert len(cases) >= 10

    lost: dict[str, list[str]] = {}
    for case in cases:
        result = await run_llm_case(
            case, counter, variant="llm+hints [live]", pipeline=pipeline, use_hints=True
        )
        assert result.error is None, result.error
        if result.wins:
            lost[case.id] = result.wins

    assert lost == {}, f"the attacks that got through: {lost}"
