"""A file cache of LLM answers, keyed by ``(model, prompt_version, request)`` (SPEC §11.3, D-33).

The evals run offline from the cache: deterministic and free. ``--live`` fills it by calling the
real client and storing what it answers. An entry holds the answer and its usage, never the
request: the prompt carries log text. Only answers that came back are stored; an error is not.

The key includes the whole ``DiagnosisRequest`` (job, excerpt, rule hints, the repair error), so a
change of the log processing, of the rules or of the hints is a different key, and so is a new
``PROMPT_VERSION`` or another model.
"""

import hashlib
import json
import os
from pathlib import Path

from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from cicd_doctor.core.ports import LLMClient


class CachedLLMClient:
    """Implements ``LLMClient``. With ``inner=None`` it only reads: a miss is an ``LLMError``."""

    def __init__(
        self,
        inner: LLMClient | None,
        directory: Path,
        *,
        model: str,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._inner = inner
        self._directory = directory
        self._model = model
        self._prompt_version = prompt_version
        self.hits = 0
        self.misses = 0

    def key(self, request: DiagnosisRequest) -> str:
        payload = {
            "model": self._model,
            "prompt_version": self._prompt_version,
            "request": request.model_dump(mode="json"),
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        path = self._directory / f"{self.key(request)}.json"
        cached = self._read(path)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        if self._inner is None:
            raise LLMError(
                f"no cached answer for this request (model {self._model}, prompt "
                f"{self._prompt_version}); run with --live to fill the cache"
            )
        result = await self._inner.diagnose(request)
        self._write(path, result)
        return result

    @staticmethod
    def _read(path: Path) -> LLMResult | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LLMResult.model_validate(data["result"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent or damaged: a miss

    def _write(self, path: Path, result: LLMResult) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "model": self._model,
            "prompt_version": self._prompt_version,
            "result": result.model_dump(mode="json"),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)  # never a half-written entry
