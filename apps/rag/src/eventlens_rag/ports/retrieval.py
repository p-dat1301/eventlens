"""Read-only retrieval storage capabilities (framework-free).

Adapters raise ``RetrievalStorageError`` for unavailable or failing storage and
must never turn such failures into empty results. Each method is one bounded,
batched read so callers can bound the number of storage round trips.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from eventlens_contracts import ArticleEnrichment
from eventlens_contracts.taxonomy import TopicTag
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.retrieval import (
    ChunkRef,
    LexicalLimits,
    LexicalPage,
    VectorHit,
)


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Filters applied identically inside both branches.

    ``published_after`` is inclusive and ``published_before`` exclusive on
    ``source_published_at``; NULL publication time is excluded once any bound
    is set. ``allowed_article_ids`` is ``None`` when no metadata filter applies.
    """

    source_domains: tuple[str, ...]
    published_after: datetime | None
    published_before: datetime | None
    allowed_article_ids: tuple[UUID, ...] | None


@dataclass(frozen=True, slots=True)
class MetadataEligibility:
    """Valid READY metadata matching topic/entity filters within a scope."""

    scoped_articles: int
    verifiable_articles: int
    matching_article_ids: tuple[UUID, ...]
    scan_cap_reached: bool


@dataclass(frozen=True, slots=True)
class CurrentArticleState:
    """Final-check view of one current, untombstoned article revision.

    ``metadata`` is set only for one valid READY publication of this exact
    revision and active index generation; otherwise it is ``None``.
    """

    document: ArticleDocument
    active_index_generation: int | None
    metadata: ArticleEnrichment | None
    metadata_generation: int | None


class RetrievalStore(Protocol):
    """Batched current-revision reads for hybrid retrieval."""

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[TopicTag, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        """Count scoped articles and list those whose valid metadata matches."""
        ...

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Bounded FTS chunk pool of current documents (one statement).

        Query and chunk terms are lexemes of the same text-search configuration
        as the stored index; at most ``chunks_scanned_per_article`` windows
        per article are scanned and, per article, only windows ranked within
        ``chunks_per_article`` by matched lexemes (ties kept) are returned.
        """
        ...

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        """Exact NFC code-point slices for the selected chunks, in order."""
        ...

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        """Exact cosine nearest active chunks of a compatible complete generation."""
        ...

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        """One batched final check of current revisions and valid metadata."""
        ...
