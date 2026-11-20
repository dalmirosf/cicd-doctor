"""A file store of LLM answers, keyed by ``(model, prompt_version, fingerprint, request)`` (D-100).

The evals run offline from the committed answers (``evals/answers/``): deterministic and free.
``--live`` fills them by calling the real client and storing what it answers. An entry holds the
answer and its usage, never the request: the prompt carries log text. Only answers that came back
are stored; an error is not.

The key includes the whole ``DiagnosisRequest`` (job, excerpt, rule hints, the repair error) and the
``prompt_fingerprint()`` (the exact system prompt and output schema), so a change of the log
processing, of the rules, of the hints, of one word of the prompt or of the schema is a different
key, and so is a new ``PROMPT_VERSION`` or another model. An entry whose stored fingerprint is not
the current one is a miss, whatever its file name says.

Entries live in two-letter subfolders (``ab/ab12....json``) so that no directory holds thousands.
"""

import hashlib
import json
import os
from pathlib import Path

from cicd_doctor.core.diagnosis.prompt import PROMPT_VERSION, prompt_fingerprint
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from cicd_doctor.core.ports import LLMClient

ENTRY_FIELDS = frozenset({"model", "prompt_version", "fingerprint", "result"})


class CachedLLMClient:
    """Implements ``LLMClient``. With ``inner=None`` it only reads: a miss is an ``LLMError``.

    ``read=False`` (``--no-cache-read``) always calls the inner client and writes the answer next
    to the official entry with ``run_suffix`` in its name, so a variance run never overwrites it.
    """

    def __init__(
        self,
        inner: LLMClient | None,
        directory: Path,
        *,
        model: str,
        prompt_version: str = PROMPT_VERSION,
        fingerprint: str | None = None,
        read: bool = True,
        run_suffix: str | None = None,
    ) -> None:
        if not read and (inner is None or not run_suffix):
            raise ValueError("reading the cache off needs a live client and a run suffix")
        self._inner = inner
        self._directory = directory
        self._model = model
        self._prompt_version = prompt_version
        self._fingerprint = fingerprint or prompt_fingerprint()
        self._read_enabled = read
        self._run_suffix = run_suffix
        self.hits = 0
        self.misses = 0
        self.used: set[Path] = set()  # the official entries this client looked up (for prune)

    def key(self, request: DiagnosisRequest) -> str:
        payload = {
            "model": self._model,
            "prompt_version": self._prompt_version,
            "fingerprint": self._fingerprint,
            "request": request.model_dump(mode="json"),
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def path_for(self, request: DiagnosisRequest) -> Path:
        key = self.key(request)
        return self._directory / key[:2] / f"{key}.json"

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        path = self.path_for(request)
        self.used.add(path)
        if self._read_enabled:
            cached = self._read(path)
            if cached is not None:
                self.hits += 1
                return cached
        self.misses += 1
        if self._inner is None:
            raise LLMError(
                f"no cached answer for this request (model {self._model}, prompt "
                f"{self._prompt_version}, fingerprint {self._fingerprint[:12]}); the answers in "
                "the repository were not filled for this prompt: run with --live to fill them"
            )
        result = await self._inner.diagnose(request)
        self._write(self._target(path), result)
        return result

    def _target(self, path: Path) -> Path:
        if self._read_enabled or not self._run_suffix:
            return path
        return path.with_name(f"{path.stem}.{self._run_suffix}.json")

    def _read(self, path: Path) -> LLMResult | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["fingerprint"] != self._fingerprint:
                return None  # an entry of another prompt (or an old one without it): a miss
            return LLMResult.model_validate(data["result"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent or damaged: a miss

    def _write(self, path: Path, result: LLMResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "model": self._model,
            "prompt_version": self._prompt_version,
            "fingerprint": self._fingerprint,
            "result": result.model_dump(mode="json"),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)  # never a half-written entry
