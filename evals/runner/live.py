"""The models of an eval run: live (OpenAI, filling the cache) or cached (offline), SPEC §11.3.

The key comes from ``OPENAI_API_KEY`` in the environment or ``.env``. It is held as a
``SecretStr``: it is not printed, logged or put in a report, and no error message here repeats it.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from cicd_doctor.adapters.embeddings.fastembed_embedder import FastEmbedEmbedder
from cicd_doctor.adapters.llm.cache import CachedLLMClient
from cicd_doctor.adapters.llm.openai_client import OpenAILLMClient, make_openai_client
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.ports import LLMClient
from evals.runner.run import EvalError, LLMSetup

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"


class LiveSettings(BaseSettings):
    """Only what an eval run reads: it needs no database."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    llm_price_input_per_mtok: Decimal | None = None
    llm_price_output_per_mtok: Decimal | None = None
    llm_max_output_tokens: int = 800
    llm_timeout_s: float = 30.0
    embedding_cache_dir: Path = Path(".cache/fastembed")
    embedding_threads: int | None = Field(default=None, ge=1)


def build_embedder(settings: LiveSettings | None = None) -> FastEmbedEmbedder:
    """The production embedder, loaded now (it downloads the model into the cache once, D-78).

    The ``llm+hints+similar`` variant is meaningless without it, so a model that does not load is
    an error here and not a silent run without embeddings.
    """
    settings = settings or LiveSettings()
    embedder = FastEmbedEmbedder(settings.embedding_cache_dir, threads=settings.embedding_threads)
    if not embedder.warm():
        raise EvalError(
            "the embedding model did not load (variant llm+hints+similar needs it; "
            f"cache dir {settings.embedding_cache_dir})"
        )
    return embedder


def parse_prices(values: Sequence[str]) -> dict[str, CostModel]:
    """``MODEL=INPUT,OUTPUT`` (USD per million tokens) for each ``--price``."""
    prices: dict[str, CostModel] = {}
    for value in values:
        model, _, pair = value.partition("=")
        parts = pair.split(",")
        try:
            if not model or len(parts) != 2:
                raise ValueError
            prices[model] = CostModel(
                input_per_mtok=Decimal(parts[0]), output_per_mtok=Decimal(parts[1])
            )
        except (ValueError, InvalidOperation):
            raise EvalError(
                f"--price {value!r}: expected MODEL=INPUT,OUTPUT in USD per Mtok"
            ) from None
    return prices


def build_setups(
    models: Sequence[str],
    *,
    live: bool,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    prices: Mapping[str, CostModel] | None = None,
    settings: LiveSettings | None = None,
) -> list[LLMSetup]:
    """One setup per model. Live ones call OpenAI and fill the cache; the others only read it."""
    settings = settings or LiveSettings()
    default_cost = CostModel(
        input_per_mtok=settings.llm_price_input_per_mtok or Decimal(0),
        output_per_mtok=settings.llm_price_output_per_mtok or Decimal(0),
    )
    sdk = None
    if live:
        if settings.openai_api_key is None:
            raise EvalError("--live needs OPENAI_API_KEY (put it in .env; it is never printed)")
        sdk = make_openai_client(
            settings.openai_api_key.get_secret_value(), timeout_s=settings.llm_timeout_s
        )
    setups = []
    for model in models:
        inner: LLMClient | None = None
        if sdk is not None:
            inner = OpenAILLMClient(
                sdk,
                model=model,
                max_output_tokens=settings.llm_max_output_tokens,
                timeout_s=settings.llm_timeout_s,
            )
        setups.append(
            LLMSetup(
                client=CachedLLMClient(inner, cache_dir, model=model),
                label=model,
                cost=(prices or {}).get(model, default_cost),
                max_output_tokens=settings.llm_max_output_tokens,
            )
        )
    return setups
