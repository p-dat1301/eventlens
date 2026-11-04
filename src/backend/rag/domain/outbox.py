"""Transactional outbox for the search projection (framework-free).

Events are written by PostgreSQL in the same transaction as the change they
describe (triggers, see revision ``rag_0003_search_platform``). A worker
claims them in batches, applies them idempotently to the search index and
marks them DONE only after the index acknowledged the write. Failures retry
with exponential backoff; after ``max_attempts`` the event is FAILED (dead
letter) and stays visible to ``index health`` and ``index reconcile``.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID


class OutboxEventType(StrEnum):
    """Change kinds the search projection reacts to."""

    ARTICLE_REVISION_CREATED = "article_revision_created"
    ARTICLE_REVISION_UPDATED = "article_revision_updated"
    ARTICLE_TOMBSTONED = "article_tombstoned"
    ENRICHMENT_PUBLISHED = "enrichment_published"
    EMBEDDING_GENERATION_ACTIVATED = "embedding_generation_activated"


class OutboxStatus(StrEnum):
    """Delivery state; FAILED is the dead-letter state."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """One claimed event; ``content_hash`` is the revision at write time."""

    event_id: int
    event_type: OutboxEventType
    article_id: UUID | None
    content_hash: str | None
    generation_id: int | None
    attempts: int
    claim_token: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxRetryPolicy:
    """Exponential backoff ``base * factor**(attempt-1)`` capped, then FAILED."""

    max_attempts: int = 6
    base_seconds: float = 5.0
    factor: float = 4.0
    cap_seconds: float = 600.0
    lease_seconds: int = 120
    batch_size: int = 100

    def delay_after(self, attempts: int) -> float | None:
        """Seconds until the next try, or ``None`` when the budget is spent."""
        if attempts >= self.max_attempts:
            return None
        return min(self.cap_seconds, self.base_seconds * self.factor ** (attempts - 1))


DEFAULT_OUTBOX_POLICY: Final = OutboxRetryPolicy()


@dataclass(frozen=True, slots=True)
class DrainReport:
    """Outcome of one drain pass (counts only, no payload)."""

    claimed: int
    done: int
    retried: int
    failed: int
    skipped_stale: int
