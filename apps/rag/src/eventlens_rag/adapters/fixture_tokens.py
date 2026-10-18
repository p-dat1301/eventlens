"""Deterministic fixture token counter (not any real model tokenizer).

Each CJK/Hangul ideograph, each run of at most 8 other word characters and
each other non-space character is one token. Counts here prove budget
mechanics only, never real model token usage (T6 owns that, D32).
"""

import re

from eventlens_rag.ports.tokens import TokenizerIdentity

_CJK = r"⺀-鿿가-힯豈-﫿\U00020000-\U0002fa1f"
_TOKEN = re.compile(rf"[{_CJK}]|[^\W{_CJK}]{{1,8}}|[^\w\s]")
FIXTURE_TOKENIZER = TokenizerIdentity(tokenizer_id="fixture-regex-tokens", version="v1")


class FixtureTokenCounter:
    """Regex token counter with exact token-boundary prefixes."""

    @property
    def identity(self) -> TokenizerIdentity:
        """Fixture tokenizer identity."""
        return FIXTURE_TOKENIZER

    def count(self, text: str) -> int:
        """Number of fixture tokens."""
        return sum(1 for _ in _TOKEN.finditer(text))

    def prefix_end(self, text: str, max_tokens: int) -> int:
        """End offset of the ``max_tokens``-th token (whole text if fewer)."""
        if max_tokens <= 0:
            return 0
        end = 0
        for index, match in enumerate(_TOKEN.finditer(text), start=1):
            end = match.end()
            if index == max_tokens:
                return end
        return len(text)
