"""Embedding boundary with fixed baseline dimension."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Versioned fixed-dimension embedding result."""

    model: str
    version: str
    values: tuple[float, ...]


class EmbeddingPort(Protocol):
    """Generate deterministic or provider-backed embeddings."""

    def embed(self, text: str) -> EmbeddingVector:
        """Embed one normalized text input."""
        ...
