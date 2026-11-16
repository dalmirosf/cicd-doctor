"""Live tests call OpenAI (SPEC §13 P1b, D-65). They run only with ``pytest -m live``.

The key and the model come from ``.env`` (``OPENAI_API_KEY``, ``OPENAI_MODEL``) and are never
printed: a test that needs them is skipped, with a message that names the variables and not their
values, when one is missing.
"""

from decimal import Decimal

import pytest

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.llm.openai_client import OpenAILLMClient, make_openai_client
from cicd_doctor.adapters.llm.tiktoken_counter import TiktokenCounter
from cicd_doctor.adapters.memory.ledger import InMemoryUsageLedger
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob
from evals.runner.live import LiveSettings

# One test never makes more calls than this, whatever the model does (a cost fence, not a quota).
MAX_CALLS_PER_TEST = 40


@pytest.fixture(scope="session")
def live_settings() -> LiveSettings:
    settings = LiveSettings()
    if settings.openai_api_key is None or not settings.openai_model:
        pytest.skip("live tests need OPENAI_API_KEY and OPENAI_MODEL in .env")
    return settings


@pytest.fixture(scope="session")
def counter() -> TiktokenCounter:
    return TiktokenCounter()


@pytest.fixture
def pipeline(live_settings: LiveSettings) -> DiagnoseJob:
    """The production pipeline over the real client, fenced to ``MAX_CALLS_PER_TEST`` calls."""
    assert live_settings.openai_api_key is not None
    assert live_settings.openai_model is not None
    sdk = make_openai_client(
        live_settings.openai_api_key.get_secret_value(), timeout_s=live_settings.llm_timeout_s
    )
    llm = OpenAILLMClient(
        sdk,
        model=live_settings.openai_model,
        max_output_tokens=live_settings.llm_max_output_tokens,
        timeout_s=live_settings.llm_timeout_s,
    )
    return DiagnoseJob(
        llm=llm,
        ledger=InMemoryUsageLedger(
            SystemClock(), calls_per_installation=MAX_CALLS_PER_TEST, daily_budget_usd=Decimal(10)
        ),
        cost=CostModel(
            input_per_mtok=live_settings.llm_price_input_per_mtok or Decimal(0),
            output_per_mtok=live_settings.llm_price_output_per_mtok or Decimal(0),
        ),
        max_output_tokens=live_settings.llm_max_output_tokens,
    )
