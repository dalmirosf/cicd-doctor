"""In-memory adapters and fakes, used by tests and offline evals."""

from cicd_doctor.adapters.memory.embedder import FakeEmbedder
from cicd_doctor.adapters.memory.installations import InMemoryInstallationStore
from cicd_doctor.adapters.memory.queue import InMemoryJobQueue
from cicd_doctor.adapters.memory.store import InMemoryFailureStore

__all__ = [
    "FakeEmbedder",
    "InMemoryFailureStore",
    "InMemoryInstallationStore",
    "InMemoryJobQueue",
]
