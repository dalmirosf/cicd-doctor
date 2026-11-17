"""The real model (D-77, D-78, SPEC §13 P2 box 1). It downloads ``model_optimized.onnx`` (66 MB)
into ``.cache/fastembed`` once, so these tests are not in the fast suite. In CI the model comes from
``actions/cache`` (see ``ci.yml``) and the tests fail instead of downloading it."""

import hashlib
import json
import math
import os
import statistics
import time
from pathlib import Path

import pytest

from cicd_doctor.adapters.embeddings.fastembed_embedder import FastEmbedEmbedder

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "fastembed"
FIXTURE = ROOT / "tests" / "fixtures" / "embeddings" / "lab_signatures.json"
# Same value as the cache key of .github/workflows/ci.yml.
ONNX_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"
MODEL_GLOB = "models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/*/model_optimized.onnx"
LATENCY_P95_S = 0.200
REPEATS = 50


def _model_file() -> Path | None:
    return next(CACHE_DIR.glob(MODEL_GLOB), None)


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    if os.environ.get("CI") and _model_file() is None:
        pytest.fail("the model is not in .cache/fastembed: the cache step of ci.yml is broken")
    instance = FastEmbedEmbedder(CACHE_DIR)
    assert instance.warm(), "the model did not load"
    return instance


@pytest.fixture(scope="module")
def items() -> list[dict[str, object]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["onnx_sha256"] == ONNX_SHA256
    return list(data["items"])


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.hypot(*a) * math.hypot(*b))


def test_the_cached_model_is_the_one_the_fixture_was_made_with(
    embedder: FastEmbedEmbedder,
) -> None:
    path = _model_file()
    assert path is not None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ONNX_SHA256


def test_vectors_have_384_dimensions_and_unit_norm(embedder: FastEmbedEmbedder) -> None:
    [vector] = embedder.embed(["ModuleNotFoundError: No module named 'requests'"])

    assert embedder.dims == len(vector) == 384
    assert math.hypot(*vector) == pytest.approx(1.0, abs=1e-5)


def test_the_same_text_gives_the_same_vector(embedder: FastEmbedEmbedder) -> None:
    text = "py-assert\nrun\nRun pytest\nassert 1 == 2"

    first, second = embedder.embed([text]), embedder.embed([text, "another text"])

    assert first[0] == second[0]  # alone and inside a batch


def test_the_fixture_vectors_are_reproduced(
    embedder: FastEmbedEmbedder, items: list[dict[str, object]]
) -> None:
    """Detects a change of platform, of fastembed or of onnxruntime (D-80, D-86)."""
    assert len(items) >= 25
    vectors = embedder.embed([str(item["signature_text"]) for item in items])

    for item, vector in zip(items, vectors, strict=True):
        expected = item["embedding"]
        assert isinstance(expected, list)
        assert _cosine(vector, expected) >= 0.9999, item["id"]
        assert max(abs(a - b) for a, b in zip(vector, expected, strict=True)) <= 1e-3, item["id"]


def test_latency_per_failure_after_the_model_is_loaded(
    embedder: FastEmbedEmbedder, items: list[dict[str, object]]
) -> None:
    """SPEC §13 P2 box 1: < 200 ms per failure on the dev machine (CI is a looser bound)."""
    texts = [str(item["signature_text"]) for item in items]
    embedder.embed(texts[:3])  # the first inference allocates
    report = []
    for batch_size in (1, 3):
        samples = []
        for i in range(REPEATS):
            batch = [texts[(i + k) % len(texts)] for k in range(batch_size)]
            started = time.perf_counter()
            embedder.embed(batch)
            samples.append(time.perf_counter() - started)
        p50 = statistics.median(samples)
        p95 = sorted(samples)[math.ceil(0.95 * len(samples)) - 1]
        report.append(f"batch of {batch_size}: p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms")
        assert p95 < LATENCY_P95_S
    print(f"embedding latency ({REPEATS} runs each): " + "; ".join(report))
