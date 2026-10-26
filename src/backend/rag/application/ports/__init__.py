"""Framework-free RAG port definitions."""

from backend.rag.application.ports.embeddings import EmbeddingPort
from backend.rag.domain.embedding import EmbeddingVector

__all__ = ["EmbeddingPort", "EmbeddingVector"]
