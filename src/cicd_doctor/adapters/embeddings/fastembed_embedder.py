"""The ``Embedder`` over fastembed (D-08, D-77, D-78).

``BAAI/bge-small-en-v1.5`` is served by fastembed from the quantised ONNX build of Qdrant
(``Qdrant/bge-small-en-v1.5-onnx-Q``, 66 MB). Loading it with the file on disk takes 0.1 s, but a
cold download took between 12 s and 166 s when it was measured, so ``embed`` never waits for it:
until the model is loaded it raises ``EmbedderUnavailable`` at once, and the loading happens in
``warm()`` (the worker runs it in a background thread) or in a background thread that ``embed``
starts itself, at most once every ``retry_after_s`` seconds after a failed attempt.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from cicd_doctor.core.errors import EmbedderUnavailable

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
RETRY_AFTER_S = 300.0


class _Model(Protocol):
    def embed(self, documents: list[str]) -> Any: ...


ModelFactory = Callable[[Path, int | None], _Model]


def load_fastembed(cache_dir: Path, threads: int | None) -> _Model:
    """Build the real model: downloads it into ``cache_dir`` when it is not there yet."""
    from fastembed import TextEmbedding  # imported here: it pulls in onnxruntime (~0.7 s)

    return TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache_dir), threads=threads)


class FastEmbedEmbedder:
    dims = DIMS

    def __init__(
        self,
        cache_dir: Path,
        *,
        threads: int | None = None,
        retry_after_s: float = RETRY_AFTER_S,
        model_factory: ModelFactory = load_fastembed,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_dir = cache_dir
        self._threads = threads
        self._retry_after_s = retry_after_s
        self._factory = model_factory
        self._now = monotonic
        self._model: _Model | None = None
        self._retry_at = 0.0
        self._load_lock = threading.Lock()  # one load at a time
        self._run_lock = threading.Lock()  # one shared instance, one inference at a time
        self._state_lock = threading.Lock()  # guards ``_loader``
        self._loader: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def warm(self) -> bool:
        """Load the model (blocking, so call it from a thread). Returns whether it is loaded.

        A failure is logged (the class of the error only) and delays the next automatic attempt
        by ``retry_after_s``; it never raises.
        """
        with self._load_lock:
            if self._model is not None:
                return True
            started = self._now()
            try:
                self._model = self._factory(self._cache_dir, self._threads)
            except Exception as exc:
                self._retry_at = self._now() + self._retry_after_s
                logger.error(
                    "embedding model failed to load (%s); retrying in at most %d s",
                    type(exc).__name__,
                    int(self._retry_after_s),
                )
                return False
            logger.info("embedding model loaded in %.1f s", self._now() - started)
            return True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One 384-d unit vector per text. Raises ``EmbedderUnavailable`` at once while the
        model is not loaded, and starts the load in the background when it is time to retry."""
        model = self._model
        if model is None:
            self._start_loading()
            raise EmbedderUnavailable("the embedding model is not loaded")
        if not texts:
            return []
        with self._run_lock:
            return [[float(x) for x in vector] for vector in model.embed(list(texts))]

    def _start_loading(self) -> None:
        with self._state_lock:
            if self._now() < self._retry_at or (self._loader and self._loader.is_alive()):
                return
            self._loader = threading.Thread(target=self.warm, name="embedding-warm", daemon=True)
            self._loader.start()
