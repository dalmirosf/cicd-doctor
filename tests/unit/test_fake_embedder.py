import math

from cicd_doctor.adapters.memory import FakeEmbedder


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors are unit length


def test_embedding_has_384_dimensions_by_default() -> None:
    embedder = FakeEmbedder()

    [vector] = embedder.embed(["ModuleNotFoundError: No module named requests"])

    assert embedder.dims == 384
    assert len(vector) == 384


def test_embedding_is_deterministic() -> None:
    first = FakeEmbedder().embed(["same text"])
    second = FakeEmbedder().embed(["same text"])

    assert first == second


def test_embedding_is_unit_length_including_for_empty_text() -> None:
    vectors = FakeEmbedder().embed(["some error text", ""])

    for vector in vectors:
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)


def test_texts_sharing_tokens_are_more_similar_than_unrelated_texts() -> None:
    embedder = FakeEmbedder()
    base, near, far = embedder.embed(
        [
            "ModuleNotFoundError No module named requests in tests",
            "ModuleNotFoundError No module named numpy in tests",
            "Error acquiring the state lock terraform backend",
        ]
    )

    assert _cosine(base, near) > 0.5
    assert _cosine(base, near) > _cosine(base, far)


def test_embed_returns_one_vector_per_text_in_order() -> None:
    embedder = FakeEmbedder(dims=16)

    vectors = embedder.embed(["a", "b", "a"])

    assert len(vectors) == 3
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
