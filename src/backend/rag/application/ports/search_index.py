"""Search-index boundary (framework-free; Elasticsearch lives in infra).

The index is a derived, rebuildable projection of PostgreSQL. Adapters raise
``SearchIndexUnavailableError`` when the engine cannot be reached and
``SearchIndexError`` for any refused or malformed request; neither may turn a
failure into an empty result. Writes return only after the engine
acknowledged them (refresh included), so a caller may mark an outbox event
DONE right after a successful write.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.retrieval_tuning import FieldBoosts
from backend.rag.domain.search_index import (
    EmbedderIdentity,
    FusedHit,
    HybridSearchConfig,
    IndexedChunk,
    IndexGeneration,
    LexicalIndexIdentity,
    RevisionDigest,
    RevisionWrite,
    ScoredChunk,
    SearchChunkDocument,
    SearchFilters,
    SearchHit,
)

__all__ = ["RevisionWrite"]


class SearchIndexError(RuntimeError):
    """The engine refused a request (message never carries payloads)."""

    code: ClassVar[str] = "search_index_failed"


class SearchIndexUnavailableError(SearchIndexError):
    """The engine cannot be reached or timed out."""

    code: ClassVar[str] = "search_index_unavailable"


@dataclass(frozen=True, slots=True)
class BulkResult:
    """Acknowledged bulk write; ``failed`` > 0 means the batch is not done.

    ``superseded`` > 0: the index already holds a newer projection of that
    article, so this (older) write was fenced out - not a failure.
    """

    indexed: int
    deleted: int
    failed: int
    superseded: int = 0


@dataclass(frozen=True, slots=True)
class WriteReport:
    """Outcome of ``write_revisions`` (counts per article and per document)."""

    applied: int
    superseded: int
    failed: int
    indexed: int
    deleted: int


@dataclass(frozen=True, slots=True)
class SearchHealth:
    """Engine reachability and the state of one generation's index."""

    reachable: bool
    engine: str
    engine_version: str | None
    index_exists: bool
    documents: int | None
    status: str


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """Generation identity plus what the index holds for it."""

    generation: IndexGeneration
    documents: int
    revisions: int
    store_bytes: int | None = None


class SearchIndexPort(Protocol):
    """Projection writes and filtered lexical/semantic chunk search."""

    @property
    def engine(self) -> str:
        """Engine label recorded in reports (e.g. ``elasticsearch``)."""
        ...

    def ensure_index(self, generation: IndexGeneration) -> None:
        """Create the generation's index with the versioned mapping if absent."""
        ...

    def drop_index(self, generation: IndexGeneration) -> None:
        """Delete the generation's index (rebuild/retire only)."""
        ...

    def write_revisions(
        self, generation: IndexGeneration, writes: tuple[RevisionWrite, ...]
    ) -> WriteReport:
        """Apply fenced article projections (see ``RevisionWrite``).

        Per article: claim the fence at ``version`` (an older version is
        superseded, never applied), write each chunk without overwriting a
        newer projection sharing its ID, re-check the fence (if a
        newer write overtook this one, delete this write's chunks), then delete
        chunks of older versions. Old chunks are never deleted when the write
        failed, so the previous state stays searchable until a retry succeeds.
        """
        ...

    def upsert_revision(
        self,
        generation: IndexGeneration,
        article_id: UUID,
        documents: tuple[SearchChunkDocument, ...],
        *,
        version: int | None = None,
    ) -> BulkResult:
        """One fenced article write (``write_revisions`` with one item).

        ``version`` defaults to the documents' ``projection_version``.
        """
        ...

    def delete_article(
        self, generation: IndexGeneration, article_id: UUID, *, version: int
    ) -> int:
        """Fenced removal of every chunk of the article (tombstone/unknown)."""
        ...

    def bulk_index(
        self, generation: IndexGeneration, documents: tuple[SearchChunkDocument, ...]
    ) -> BulkResult:
        """Index many chunks (rebuild path); ``failed`` reports refused items."""
        ...

    def lexical_search(
        self,
        generation: IndexGeneration,
        query: str,
        filters: SearchFilters,
        depth: int,
        boosts: FieldBoosts | None = None,
    ) -> tuple[SearchHit, ...]:
        """BM25 over title/sapo/text with non-scoring filters, best first.

        ``boosts`` ``None`` = the mapping default title^3, sapo^1.5, text^1.
        """
        ...

    def semantic_search(
        self,
        generation: IndexGeneration,
        vector: tuple[float, ...],
        filters: SearchFilters,
        depth: int,
    ) -> tuple[SearchHit, ...]:
        """Cosine kNN over the generation's vectors with the same filters."""
        ...

    def hybrid_search(
        self,
        generation: IndexGeneration,
        query: str,
        vector: tuple[float, ...] | None,
        filters: SearchFilters,
        config: HybridSearchConfig,
    ) -> tuple[FusedHit, ...]:
        """BM25 top-N and kNN top-N with identical filters, fused by RRF.

        ``vector=None`` runs the lexical branch only. Raw scores are never
        summed across branches.
        """
        ...

    def health(self, generation: IndexGeneration | None) -> SearchHealth:
        """Reachability and, if given, the generation index state."""
        ...

    def status(self, generation: IndexGeneration) -> IndexStatus:
        """Document and revision counts of the generation's index."""
        ...

    def revision_digests(
        self, generation: IndexGeneration
    ) -> tuple[RevisionDigest, ...]:
        """Per-article ``(content_hash, chunks)`` held by the index (reconcile)."""
        ...


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
