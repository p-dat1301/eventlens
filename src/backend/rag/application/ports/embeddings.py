"""Embedding boundary with fixed baseline dimension."""

from collections.abc import Sequence
from typing import Protocol

from backend.rag.domain.embedding import EmbeddingVector
from backend.rag.domain.search_embedding import SearchEmbeddingIdentity


class EmbeddingPort(Protocol):
    """Generate deterministic or provider-backed embeddings."""

    def embed(self, text: str) -> EmbeddingVector:
        """Embed one normalized text input."""
        ...


class BatchEmbeddingPort(Protocol):
    """Search-index embeddings with an explicit model identity.

    Implementations must return exactly ``identity.dimensions`` finite values
    per text and never download weights at call time.
    """

    @property
    def identity(self) -> SearchEmbeddingIdentity:
        """Model identity recorded on generations and cached vectors."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """One unit-length vector per document text, in order."""
        ...

    def embed_query(self, text: str) -> tuple[float, ...]:
        """One unit-length query vector."""
        ...
