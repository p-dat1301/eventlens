"""Embedding boundary with fixed baseline dimension."""

from typing import Protocol

from backend.rag.domain.embedding import EmbeddingVector


class EmbeddingPort(Protocol):
    """Generate deterministic or provider-backed embeddings."""

    def embed(self, text: str) -> EmbeddingVector:
        """Embed one normalized text input."""
        ...
