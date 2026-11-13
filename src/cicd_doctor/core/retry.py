"""Retry bookkeeping for queued jobs (SPEC §8, D-23, D-49). Pure functions, no IO."""

from datetime import timedelta

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_FACTOR = 4
BACKOFF_CAP = timedelta(minutes=30)
LAST_ERROR_MAX_BYTES = 2048  # jobs.last_error is truncated to 2 KB (SPEC §6)


def backoff_ceiling(attempt: int) -> timedelta:
    """Upper bound of the wait after failed attempt number ``attempt`` (1-based).

    30 s, 2 min, 8 min, then 30 min for every later attempt.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    cap = int(BACKOFF_CAP.total_seconds())
    base = int(BACKOFF_BASE.total_seconds())
    # Compare before multiplying out the timedelta, so huge attempt numbers cannot overflow.
    seconds = min(cap, base * BACKOFF_FACTOR ** min(attempt - 1, 32))
    return timedelta(seconds=seconds)


def backoff_delay(attempt: int, jitter: float) -> timedelta:
    """Full-jitter backoff: ``jitter`` is a random value in [0, 1] chosen by the caller."""
    if not 0.0 <= jitter <= 1.0:
        raise ValueError(f"jitter must be within [0, 1], got {jitter}")
    return backoff_ceiling(attempt) * jitter


def truncate_error(text: str, max_bytes: int = LAST_ERROR_MAX_BYTES) -> str:
    """Cut ``text`` to at most ``max_bytes`` of UTF-8 without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
