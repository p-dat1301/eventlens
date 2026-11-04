"""Ports for search-selection engines (BM25 index, embedder, vector index).

Workflows and evaluation depend only on these protocols; Elasticsearch, model
runtimes and pgvector DDL stay in ``infra``. Every search applies the same
``RetrievalScope`` filters inside the engine, before ranking depth is cut.
"""

from collections.abc import Sequence
from typing import Protocol

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.search_index import (
    EmbedderIdentity,
    IndexedChunk,
    LexicalIndexIdentity,
    ScoredChunk,
)


class ChunkLexicalIndex(Protocol):
    """Lexical (BM25) chunk index rebuilt from one corpus snapshot."""

    @property
    def identity(self) -> LexicalIndexIdentity:
        """Engine/analyzer identity of this index."""
        ...

    def rebuild(self, chunks: Sequence[IndexedChunk]) -> None:
        """Replace the index content with exactly these chunks."""
        ...

    def search(
        self, query: str, scope: RetrievalScope, limit: int
    ) -> tuple[ScoredChunk, ...]:
        """Top ``limit`` chunks by engine score within ``scope``."""
        ...

    def size_bytes(self) -> int:
        """On-disk size of the index (primary store)."""
        ...


class TextEmbedder(Protocol):
    """Batch embedder; query and document inputs may use different prefixes."""

    @property
    def identity(self) -> EmbedderIdentity:
        """Model identity, revision and dimension."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """One vector of ``identity.dimension`` finite floats per text."""
        ...

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Query-side vectors (with the model's query instruction, if any)."""
        ...


class ChunkVectorIndex(Protocol):
    """Exact cosine search over vectors of one ``EmbedderIdentity``."""

    def rebuild(
        self,
        identity: EmbedderIdentity,
        chunks: Sequence[IndexedChunk],
        vectors: Sequence[tuple[float, ...]],
    ) -> None:
        """Replace this identity's vectors with exactly these chunks."""
        ...

    def search(
        self,
        identity: EmbedderIdentity,
        vector: tuple[float, ...],
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Exact top ``limit`` current chunks by cosine similarity in scope."""
        ...

    def size_bytes(self, identity: EmbedderIdentity) -> int:
        """Total relation size of this identity's vectors."""
        ...


class CorpusReader(Protocol):
    """Read the current, untombstoned corpus snapshot (benchmark indexing)."""

    def current_documents(self) -> tuple[ArticleDocument, ...]:
        """Every current revision, ordered by article ID."""
        ...
