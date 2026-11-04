"""PostgreSQL-side ports of the search projection (framework-free).

PostgreSQL owns documents, revisions, tombstones, enrichment and the outbox.
These ports read that truth and record delivery state; nothing here writes
article data.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.outbox import OutboxEvent, OutboxStatus
from backend.rag.domain.search_embedding import SearchEmbeddingIdentity
from backend.rag.domain.search_index import IndexGeneration, RevisionDigest
from backend.shared.contracts import ArticleEnrichment


@dataclass(frozen=True, slots=True)
class ProjectionRevision:
    """Current state of one article as PostgreSQL sees it.

    ``document`` is ``None`` when the article is unknown. ``metadata`` is set
    only for one valid READY publication of exactly this revision.
    ``version`` orders states of one article: the highest outbox event ID of
    the article, read in the same snapshot as the state (every state change
    writes an event in its own transaction, so a newer state always has a
    larger version; 0 = no event yet, i.e. data from before the outbox).
    """

    article_id: UUID
    document: ArticleDocument | None
    tombstoned: bool
    metadata: ArticleEnrichment | None
    version: int = 0


class SearchProjectionSource(Protocol):
    """Batched reads of current revisions (the only projection input)."""

    def revision(self, article_id: UUID) -> ProjectionRevision:
        """Current revision of one article (tombstone and metadata included)."""
        ...

    def current_page(
        self, after: UUID | None, limit: int
    ) -> tuple[ProjectionRevision, ...]:
        """Untombstoned current revisions ordered by ``article_id`` (keyset)."""
        ...

    def current_digests(self) -> tuple[RevisionDigest, ...]:
        """``(article_id, content_hash)`` of every untombstoned current revision.

        ``chunks`` is ``-1``: PostgreSQL does not store search chunks; reconcile
        recomputes the expected count from the chunker.
        """
        ...


class OutboxStore(Protocol):
    """Claim/ack of outbox events; every write is its own short transaction."""

    def claim(self, limit: int, lease_seconds: int) -> tuple[OutboxEvent, ...]:
        """Claim due PENDING (or lease-expired PROCESSING) events, oldest first."""
        ...

    def complete(self, events: Sequence[OutboxEvent]) -> int:
        """Mark claimed events DONE (matching claim token only)."""
        ...

    def retry(self, event: OutboxEvent, error_code: str, delay_seconds: float) -> bool:
        """Back to PENDING after ``delay_seconds`` (matching claim token only)."""
        ...

    def fail(self, event: OutboxEvent, error_code: str) -> bool:
        """Dead-letter: FAILED with the error code (matching claim token only)."""
        ...

    def counts(self) -> dict[OutboxStatus, int]:
        """Events per status (health)."""
        ...

    def oldest_pending(self) -> datetime | None:
        """Creation time of the oldest PENDING event (lag)."""
        ...

    def requeue_failed(self) -> int:
        """Operator action: FAILED back to PENDING with attempts reset."""
        ...


class SearchGenerationStore(Protocol):
    """Index generations recorded in PostgreSQL (identity + lifecycle)."""

    def active(self) -> IndexGeneration | None:
        """The one ACTIVE generation, if any."""
        ...

    def writable(self) -> tuple[IndexGeneration, ...]:
        """ACTIVE and BUILDING generations (outbox events apply to both)."""
        ...

    def create(
        self,
        chunk_version: str,
        embedding: SearchEmbeddingIdentity | None,
        index_prefix: str,
    ) -> IndexGeneration:
        """New BUILDING generation with a unique index name."""
        ...

    def activate(self, generation_id: int) -> IndexGeneration:
        """Make one BUILDING generation ACTIVE and retire the previous one.

        Emits ``embedding_generation_activated`` in the same transaction.
        """
        ...

    def mark_failed(self, generation_id: int) -> None:
        """Interrupted or refused build; never becomes ACTIVE."""
        ...

    def get(self, generation_id: int) -> IndexGeneration | None:
        """One generation by ID."""
        ...


class EmbeddingCache(Protocol):
    """Vectors keyed by ``(text_hash, embedding identity)`` in PostgreSQL."""

    def get_many(
        self, text_hashes: Sequence[str], identity: SearchEmbeddingIdentity
    ) -> dict[str, tuple[float, ...]]:
        """Cached vectors for the hashes that exist (dimension-checked)."""
        ...

    def put_many(
        self,
        vectors: Sequence[tuple[str, tuple[float, ...]]],
        identity: SearchEmbeddingIdentity,
    ) -> int:
        """Insert missing vectors (idempotent); returns rows written."""
        ...
