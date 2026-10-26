"""Tokenizer identity value object."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenizerIdentity:
    """Tokenizer/policy identity recorded with every counted context."""

    tokenizer_id: str
    version: str
