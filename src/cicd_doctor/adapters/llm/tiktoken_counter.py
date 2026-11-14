"""Token counting with tiktoken (SPEC §7.3 step 7, D-57).

tiktoken downloads the BPE file of an encoding the first time it is used and caches it on disk
(``TIKTOKEN_CACHE_DIR``, else a ``data-gym-cache`` folder in the temp directory). That download
is why this lives in an adapter and not in ``core``.
"""

from functools import cached_property

import tiktoken

DEFAULT_ENCODING = "o200k_base"  # the encoding of the current OpenAI models


class TiktokenCounter:
    """Counts tokens with a tiktoken encoding, loaded on first use."""

    def __init__(self, encoding_name: str = DEFAULT_ENCODING) -> None:
        self._encoding_name = encoding_name

    @cached_property
    def _encoding(self) -> tiktoken.Encoding:
        return tiktoken.get_encoding(self._encoding_name)

    def count(self, text: str) -> int:
        # encode_ordinary: special-token text such as "<|endoftext|>" in a log is plain text here.
        return len(self._encoding.encode_ordinary(text))
