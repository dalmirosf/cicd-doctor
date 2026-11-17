"""``llm+hints+similar`` with the real embedding model over real lab cases (D-86).

Like ``test_fastembed.py`` it needs the 66 MB model in ``.cache/fastembed`` (CI caches it) and is
not in the fast suite. The LLM is a FakeLLM: nothing here calls OpenAI.
"""

import os
from decimal import Decimal
from pathlib import Path

import pytest

from cicd_doctor.adapters.embeddings.fastembed_embedder import FastEmbedEmbedder
from cicd_doctor.adapters.memory import CharTokenCounter, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis.cost import CostModel
from evals.runner.cases import DEFAULT_CASES_DIR, load_case
from evals.runner.run import LLMSetup, run_evals_async
from tests.adapters.test_fastembed import CACHE_DIR, _model_file

CASES = (
    "lab-py-assert-01",
    "lab-py-assert-02",
    "lab-py-modnf-01",
    "lab-py-modnf-02",
    "lab-py-mypy-01",
    "lab-py-ruff-01",
)


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    if os.environ.get("CI") and _model_file() is None:
        pytest.fail("the model is not in .cache/fastembed: the cache step of ci.yml is broken")
    instance = FastEmbedEmbedder(CACHE_DIR)
    assert instance.warm(), "the model did not load"
    return instance


async def test_the_repeated_captures_hit_the_cache_and_every_similar_score_is_real(
    embedder: FastEmbedEmbedder, tmp_path: Path
) -> None:
    for name in CASES:  # a directory with just these cases
        source = tmp_path / "lab" / name
        source.mkdir(parents=True)
        for file in (DEFAULT_CASES_DIR / "lab" / name).iterdir():
            (source / file.name).write_bytes(file.read_bytes())
    fake = FakeLLM([valid()] * len(CASES))
    setup = LLMSetup(
        client=fake,
        label="m",
        cost=CostModel(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1)),
    )

    report = await run_evals_async(
        "llm+hints+similar",
        ["lab"],
        tmp_path,
        CharTokenCounter(),
        llms=[setup],
        embedder=embedder,
    )

    [run] = report.runs
    results = {r.case_id: r for r in run.results}
    assert len(results) == len(CASES)
    # Two captures of one failure share a signature: the later one is served from the cache.
    for pair in (("lab-py-assert-01", "lab-py-assert-02"), ("lab-py-modnf-01", "lab-py-modnf-02")):
        sources = sorted(results[name].diagnosis_source for name in pair)
        assert sources == ["cache", "llm"], pair
    assert len(fake.calls) == len(CASES) - 2
    assert run.metrics.cache_hits == 2
    for result in results.values():
        assert result.similar_top_score is None or 0.85 <= result.similar_top_score <= 1.0
    assert load_case(tmp_path / "lab" / CASES[0]).meta.captured_at is not None
