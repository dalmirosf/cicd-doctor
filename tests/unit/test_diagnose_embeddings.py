"""The embedding step of the diagnosis flow (P2 a, D-77): one batch per attempt, before the LLM,
stored in the checkpoint, and never a reason to fail or retry the job."""

import asyncio
import logging
import threading
import time
from collections.abc import Sequence

import pytest

from cicd_doctor.adapters.memory import FakeEmbedder
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.errors import EmbedderUnavailable
from tests.unit.test_diagnose_failed_run import World, log_text, workflow_job


class RecordingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.threads: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        self.threads.append(threading.get_ident())
        return super().embed(texts)


class BrokenEmbedder:
    dims = 384

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise self.error


class SlowEmbedder(FakeEmbedder):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        time.sleep(0.3)
        return super().embed(texts)


def two_failed_jobs(world: World) -> None:
    world.logs.add_jobs(world.run, [workflow_job(11), workflow_job(12, name="lint")])
    world.logs.add_log(11, log_text())
    world.logs.add_log(12, log_text(["ruff: 3 errors"]))


async def test_the_checkpoint_carries_the_embedding_of_the_signature_text() -> None:
    embedder = RecordingEmbedder()
    world = World(embedder=embedder)

    await world.run_once()

    [item] = (await world.record()).items
    assert item.failure.embedding == FakeEmbedder().embed([item.failure.signature.text])[0]
    assert len(item.failure.embedding or []) == 384


async def test_two_jobs_are_embedded_in_one_batch_off_the_event_loop() -> None:
    embedder = RecordingEmbedder()
    world = World([valid(), valid()], jobs=False, embedder=embedder)
    two_failed_jobs(world)

    await world.run_once()

    items = (await world.record()).items
    assert len(items) == 2
    assert len(embedder.batches) == 1
    assert sorted(embedder.batches[0]) == sorted(i.failure.signature.text for i in items)
    assert embedder.threads[0] != threading.get_ident()
    assert all(i.failure.embedding for i in items)


async def test_the_embedding_is_computed_before_the_llm_is_called() -> None:
    embedder = RecordingEmbedder()
    world = World(embedder=embedder)
    seen: list[int] = []
    original = world.llm.diagnose

    async def spy(request):  # type: ignore[no-untyped-def]
        seen.append(len(embedder.batches))
        return await original(request)

    world.llm.diagnose = spy  # type: ignore[method-assign]

    await world.run_once()

    assert seen == [1]


@pytest.mark.parametrize(
    "error", [EmbedderUnavailable("not loaded"), RuntimeError("onnx exploded with SECRET-TEXT")]
)
async def test_an_embedder_error_stores_no_vector_and_publishes_anyway(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = BrokenEmbedder(error)
    world = World(embedder=embedder)

    with caplog.at_level(logging.WARNING):
        await world.run_once()  # does not raise: the job does not retry

    [item] = (await world.record()).items
    assert item.failure.embedding is None
    assert item.published.source == "llm"
    assert await world.store.publication(world.check_key()) is not None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(type(error).__name__ in message for message in warnings)
    assert not any("SECRET-TEXT" in message or "ModuleNotFound" in message for message in warnings)
    assert embedder.calls == 1


async def test_a_wrong_shape_from_the_embedder_is_treated_as_no_embedding() -> None:
    class Short(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0, 0.0]]

    world = World(embedder=Short())

    await world.run_once()

    [item] = (await world.record()).items
    assert item.failure.embedding is None


async def test_without_an_embedder_everything_works_as_before() -> None:
    world = World(embedder=None)

    await world.run_once()

    [item] = (await world.record()).items
    assert item.failure.embedding is None
    assert item.published.source == "llm"


async def test_a_retry_after_the_checkpoint_does_not_call_the_embedder_again() -> None:
    embedder = RecordingEmbedder()
    world = World(embedder=embedder)
    world.store.fail_comment_saves = 1

    with pytest.raises(Exception, match="database went away"):
        await world.run_once()
    await world.run_once()

    assert len(embedder.batches) == 1


async def test_a_slow_embedder_does_not_block_the_event_loop() -> None:
    world = World(embedder=SlowEmbedder())
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await world.run_once()
    finally:
        task.cancel()

    assert ticks > 10  # a blocked loop would have counted almost nothing in 0.3 s
