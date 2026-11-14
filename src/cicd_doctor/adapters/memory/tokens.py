"""A deterministic, offline stand-in for the tiktoken counter (D-57)."""

import math


class CharTokenCounter:
    """About four characters per token, at least one token per non-empty text.

    No encoding file and no network, so the fast suite runs offline. Real token counts come from
    ``adapters.llm.tiktoken_counter.TiktokenCounter``.
    """

    def __init__(self, chars_per_token: int = 4) -> None:
        if chars_per_token < 1:
            raise ValueError("chars_per_token must be >= 1")
        self._chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token) if text else 0
