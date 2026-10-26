"""Immutable storage identities and fenced job claims."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ArticleRevision:
    """One immutable searchable article revision."""

    article_id: UUID
    content_hash: str
    normalized_content: str
    indexed_at: datetime


@dataclass(frozen=True, slots=True)
class EnrichmentJobKey:
    """Five-field idempotency identity for enrichment work."""

    article_id: UUID
    content_hash: str
    enrichment_version: str
    model_version: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Durable job state with monotonic ownership generation."""

    job_id: UUID
    key: EnrichmentJobKey
    generation: int
    complete: bool = False

    @classmethod
    def pending(cls, key: EnrichmentJobKey) -> "JobClaim":
        """Create initial pending job."""
        return cls(job_id=uuid4(), key=key, generation=0)
