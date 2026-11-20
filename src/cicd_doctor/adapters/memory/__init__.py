"""In-memory adapters and fakes, used by tests and offline evals."""

from cicd_doctor.adapters.memory.command_log import InMemoryCommandLog
from cicd_doctor.adapters.memory.embedder import FakeEmbedder
from cicd_doctor.adapters.memory.github import (
    FakeLogSource,
    FakePullRequests,
    FakeReporter,
    FakeRunSource,
)
from cicd_doctor.adapters.memory.installations import InMemoryInstallationStore
from cicd_doctor.adapters.memory.ledger import InMemoryUsageLedger
from cicd_doctor.adapters.memory.llm import FakeLLM
from cicd_doctor.adapters.memory.queue import InMemoryJobQueue
from cicd_doctor.adapters.memory.store import InMemoryFailureStore
from cicd_doctor.adapters.memory.tokens import CharTokenCounter

__all__ = [
    "CharTokenCounter",
    "FakeEmbedder",
    "FakeLLM",
    "FakeLogSource",
    "FakePullRequests",
    "FakeReporter",
    "FakeRunSource",
    "InMemoryCommandLog",
    "InMemoryFailureStore",
    "InMemoryInstallationStore",
    "InMemoryJobQueue",
    "InMemoryUsageLedger",
]
