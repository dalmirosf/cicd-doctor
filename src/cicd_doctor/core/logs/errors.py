"""Error-line detection (SPEC §7.3 step 5).

The generic patterns live here; the rule patterns (SPEC §10) are passed in by the caller, so the
excerpt keeps every line a rule could match.
"""

import re
from collections.abc import Sequence

# Only the first characters of a line are searched; rules use the same cap (SPEC §10).
MAX_SCAN_CHARS = 2000

ERROR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"##\[error\]",
        r"Error:",  # also "ModuleNotFoundError:", "AssertionError:"
        r"\bERROR\b",
        r"\bFAILED\b",
        r"\bFAIL\b",
        r"\bTraceback\b",
        r"Exception\b",
        r"\bfatal:",
        r"\bpanic:",
        r"Process completed with exit code",
        r"\bexit status\b",
        r"\bKilled\b",
    )
)

_GENERIC = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in ERROR_PATTERNS))


def is_error_line(text: str, extra: Sequence[re.Pattern[str]] = ()) -> bool:
    """True when the line matches a generic error pattern or one of the ``extra`` patterns."""
    scanned = text[:MAX_SCAN_CHARS]
    if _GENERIC.search(scanned):
        return True
    return any(pattern.search(scanned) for pattern in extra)
