"""A deterministic stand-in for the real embedder (fastembed, D-08)."""

import hashlib
import math
from collections.abc import Sequence


class FakeEmbedder:
    """Feature-hashing embedder: no model, no network, same output on every machine.

    Each whitespace-separated token is hashed to one of ``dims`` buckets with a sign, and the
    vector is L2-normalised. Identical texts give identical vectors, and texts that share tokens
    have a higher cosine similarity than unrelated ones, which is enough to test similarity logic.
    """

    def __init__(self, dims: int = 384) -> None:
        if dims < 1:
            raise ValueError("dims must be positive")
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for token in text.split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dims
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0  # empty text (or fully cancelled tokens): a fixed unit vector
            return vector
        return [value / norm for value in vector]
