"""In-memory adapters and fakes, used by tests and offline evals."""

from cicd_doctor.adapters.memory.embedder import FakeEmbedder
from cicd_doctor.adapters.memory.store import InMemoryFailureStore

__all__ = ["FakeEmbedder", "InMemoryFailureStore"]
