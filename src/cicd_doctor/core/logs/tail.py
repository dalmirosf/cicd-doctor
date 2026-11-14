"""Tail-biased log retention: keep the last ``LOG_MAX_BYTES`` of a job log (D-11, FR-07).

The GitHub log adapter (P1c) streams the download into a ``TailBuffer`` when it cannot ask for
the tail with an HTTP ``Range`` request. ``build_excerpts`` also caps the text it receives with
``tail_text``, so the limit holds whatever the log source did.

When bytes are dropped from the front, the retained text starts at the first complete line, so
line numbers in the excerpt count from the start of the retained tail (D-58).
"""

from collections import deque

from cicd_doctor.core.models import RawLog

DEFAULT_MAX_BYTES = 20 * 1024 * 1024  # LOG_MAX_BYTES
HARD_READ_LIMIT = 100 * 1024 * 1024  # stop reading a log without Content-Length after 100 MB


class TailBuffer:
    """Accumulates chunks and keeps only the last ``max_bytes`` of them.

    ``feed`` returns False once ``hard_limit`` bytes have been read: the caller must stop
    reading. Memory stays bounded by ``max_bytes`` plus one chunk.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES, hard_limit: int = HARD_READ_LIMIT):
        if max_bytes < 1 or hard_limit < max_bytes:
            raise ValueError("need 1 <= max_bytes <= hard_limit")
        self._max_bytes = max_bytes
        self._hard_limit = hard_limit
        self._chunks: deque[bytes] = deque()
        self._size = 0  # bytes held in _chunks
        self._read = 0  # bytes accepted so far
        self._stopped = False  # the hard limit cut the end

    @property
    def bytes_read(self) -> int:
        return self._read

    def feed(self, chunk: bytes) -> bool:
        """Add a chunk. Returns False when the hard read limit has been reached."""
        if self._stopped:
            return False
        room = self._hard_limit - self._read
        if len(chunk) >= room:
            chunk = chunk[:room]
            self._stopped = True
        if chunk:
            self._chunks.append(chunk)
            self._size += len(chunk)
            self._read += len(chunk)
            self._trim()
        return not self._stopped

    def _trim(self) -> None:
        # Drop whole chunks while the rest still holds max_bytes plus the byte before them (it
        # tells whether the tail starts at a line boundary); the first chunk is cut in result().
        while self._chunks and self._size - len(self._chunks[0]) > self._max_bytes:
            self._size -= len(self._chunks.popleft())

    def result(self) -> RawLog:
        """The retained tail, decoded as UTF-8 (invalid bytes replaced)."""
        data, cut = _tail(b"".join(self._chunks), self._max_bytes)
        return RawLog(text=data.decode("utf-8", errors="replace"), truncated=cut or self._stopped)


def tail_text(text: str, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, bool]:
    """Keep the last ``max_bytes`` of UTF-8 of ``text``. Returns ``(text, truncated)``."""
    if len(text) * 4 <= max_bytes:  # a str of n chars is at most 4n bytes of UTF-8
        return text, False
    data, cut = _tail(text.encode("utf-8"), max_bytes)
    return (data.decode("utf-8", errors="replace"), True) if cut else (text, False)


def _tail(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """The last ``max_bytes`` of ``data``, starting at a complete line, and whether it was cut.

    When the cut falls inside a line, that partial line is dropped. A tail without any newline is
    one giant line: it is kept as is rather than dropped.
    """
    if len(data) <= max_bytes:
        return data, False
    at_line_start = data[len(data) - max_bytes - 1] == ord("\n")
    tail = data[len(data) - max_bytes :]
    if not at_line_start:
        newline = tail.find(b"\n")
        if newline >= 0:
            tail = tail[newline + 1 :]
    return tail, True
