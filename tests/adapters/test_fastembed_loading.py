"""Loading, failing fast and retrying of ``FastEmbedEmbedder`` (D-78), with a fake model: no
download, no onnxruntime."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cicd_doctor.adapters.embeddings.fastembed_embedder import FastEmbedEmbedder
from cicd_doctor.core.errors import EmbedderUnavailable


class Model:
    def embed(self, documents: list[str]) -> Any:
        return ([float(len(d)), 0.5] for d in documents)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Factory:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.args: tuple[Path, int | None] | None = None
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, cache_dir: Path, threads: int | None) -> Model:
        self.calls += 1
        self.args = (cache_dir, threads)
        self.gate.wait(5)
        if self.calls <= self.failures:
            raise OSError("offline: https://huggingface.co/secret-url")
        return Model()


def make(factory: Factory, clock: Clock | None = None) -> FastEmbedEmbedder:
    return FastEmbedEmbedder(
        Path("cache"),
        threads=2,
        retry_after_s=300,
        model_factory=factory,
        monotonic=clock or Clock(),
    )


def wait_until(condition: Any) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_warm_loads_the_model_once_with_the_cache_dir_and_threads() -> None:
    factory = Factory()
    embedder = make(factory)

    assert embedder.warm()
    assert embedder.warm()

    assert factory.calls == 1
    assert factory.args == (Path("cache"), 2)
    assert embedder.ready
    assert embedder.embed(["ab", "abc"]) == [[2.0, 0.5], [3.0, 0.5]]
    assert embedder.embed([]) == []


def test_embed_fails_fast_while_the_model_downloads_and_never_waits_for_it() -> None:
    factory = Factory()
    factory.gate.clear()  # the download hangs
    embedder = make(factory)
    started = time.monotonic()

    with pytest.raises(EmbedderUnavailable):
        embedder.embed(["x"])
    with pytest.raises(EmbedderUnavailable):
        embedder.embed(["x"])

    assert time.monotonic() - started < 1
    wait_until(lambda: factory.calls == 1)  # the first call started one background load
    factory.gate.set()
    wait_until(lambda: embedder.ready)
    assert factory.calls == 1
    assert embedder.embed(["x"]) == [[1.0, 0.5]]


def test_a_failed_load_is_retried_at_most_every_five_minutes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = Factory(failures=1)
    clock = Clock()
    embedder = make(factory, clock)

    with caplog.at_level("ERROR"):
        assert embedder.warm() is False
    assert "OSError" in caplog.text
    assert "huggingface" not in caplog.text  # only the class of the error

    clock.now += 299
    with pytest.raises(EmbedderUnavailable):
        embedder.embed(["x"])
    time.sleep(0.1)
    assert factory.calls == 1  # too soon

    clock.now += 2
    with pytest.raises(EmbedderUnavailable):
        embedder.embed(["x"])  # still raises at once, and starts the retry
    wait_until(lambda: embedder.ready)
    assert factory.calls == 2
    assert embedder.embed(["x"]) == [[1.0, 0.5]]


def test_warm_never_raises() -> None:
    assert make(Factory(failures=5)).warm() is False
