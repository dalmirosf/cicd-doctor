# ruff: noqa
"""Throwaway (H1): prints ids and scores of the similar lists, never text."""

import platform
import sys

from cicd_doctor.adapters.memory.store import _cosine
from evals.runner import history as H

olook = H.SimilarHistory.lookup


async def lookup(self, case, analysis):
    r = await olook(self, case, analysis)
    repo = self._repo(case)
    for rank, s in enumerate(r.similar):
        f = repo.store._failures[s.failure_id]
        v = _cosine(r.embedding, f.embedding)
        print(
            f"H1 {case.id} rank{rank} {str(s.failure_id)[:8]} stored={s.score} raw={v:.9f}",
            flush=True,
        )
    return r


H.SimilarHistory.lookup = lookup
import onnxruntime

print("H1 onnxruntime", onnxruntime.__version__, platform.machine(), flush=True)
from evals.runner.cli import main

sys.exit(main(["check"]))
