"""Reuse a cached LLM diagnosis for a new failure with the same signature (SPEC §9.4, D-81).

The cached text was verified against the excerpt of the run that produced it. The new run has the
same signature, not necessarily the same lines (other line numbers, other timestamps already
stripped by the cleaning). So every cited line is found again **by its text** in the new excerpt:
what is published is still a quote of the log that is shown. A quote that is gone is dropped, and
when none is left the cache does not apply (the normal policy goes on and calls the model).
"""

from cicd_doctor.core.diagnosis.validate import normalise_quote, quotes_line
from cicd_doctor.core.models import Diagnosis, Evidence, LogExcerpt


def reuse_cached(cached: Diagnosis, excerpt: LogExcerpt) -> Diagnosis | None:
    """The cached diagnosis as it is published for ``excerpt``, or ``None`` when it does not apply.

    Each evidence is re-anchored to the line of the new excerpt that contains its text (the one
    closest to the old line number among those not used yet) and renumbered. ``suggestion`` is
    dropped (its path and lines belong to another commit). ``source`` becomes ``cache``; the
    model, the prompt version, the category, the texts and the confidence are kept.
    """
    anchored: list[Evidence] = []
    seen: set[int] = set()
    for item in cached.evidence:
        quoted = normalise_quote(item.text)
        if not quoted:
            continue
        candidates = [
            line.number
            for line in excerpt.lines
            if line.number not in seen and quotes_line(quoted, line.text)
        ]
        if not candidates:
            continue
        number = min(candidates, key=lambda n: (abs(n - item.line), n))
        seen.add(number)
        anchored.append(Evidence(line=number, text=item.text))
    if not anchored:
        return None
    return cached.model_copy(
        update={
            "evidence": anchored,
            "suggestion": None,
            "source": "cache",
            "evidence_verified": True,
        }
    )
