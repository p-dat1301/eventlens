"""Minimal deterministic token-counting contract for evidence budgets.

T6 owns the full model-input ledger (D32); it reuses this port so there is one
token-counting boundary, not two competing ones.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TokenizerIdentity:
    """Tokenizer/policy identity recorded with every counted context."""

    tokenizer_id: str
    version: str


class TokenCounter(Protocol):
    """Count tokens and find token-boundary prefixes of exact text."""

    @property
    def identity(self) -> TokenizerIdentity:
        """Tokenizer identity."""
        ...

    def count(self, text: str) -> int:
        """Number of tokens in ``text``."""
        ...

    def prefix_end(self, text: str, max_tokens: int) -> int:
        """Code-point end of the longest prefix holding at most ``max_tokens``.

        The prefix ends exactly at a token boundary, so ``text[:end]`` is an
        unmodified substring of the original.
        """
        ...
