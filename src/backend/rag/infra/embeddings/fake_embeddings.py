"""Deterministic local-only embedding adapter."""

from hashlib import sha256

from backend.rag.domain.embedding import EmbeddingVector

DIMENSION = 1536


class FakeEmbeddings:
    """Map text deterministically to normalized 1536-dimension values."""

    def embed(self, text: str) -> EmbeddingVector:
        """Create stable fake vector without network or provider credentials."""
        values = tuple(
            (int.from_bytes(sha256(f"{text}:{index}".encode()).digest()[:2]) / 32767.5)
            - 1.0
            for index in range(DIMENSION)
        )
        return EmbeddingVector(
            model="fixture-embedding", version="fixture-embedding.v1", values=values
        )
