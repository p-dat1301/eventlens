"""Deterministic in-memory semantics mirroring storage invariants."""

from dataclasses import dataclass, field
from uuid import UUID

from backend.rag.domain.records import ArticleRevision, EnrichmentJobKey, JobClaim


@dataclass(slots=True)
class InMemoryRagStore:  # noqa: MUTABLE_OK
    """Test fake for immutable revisions, tombstones, and generation fences."""

    _revisions: dict[UUID, list[ArticleRevision]] = field(default_factory=dict)
    _tombstones: set[UUID] = field(default_factory=set)
    _jobs_by_key: dict[EnrichmentJobKey, JobClaim] = field(default_factory=dict)
    _jobs_by_id: dict[UUID, JobClaim] = field(default_factory=dict)

    def persist_article(self, revision: ArticleRevision) -> None:
        """Append new immutable revision and restore current visibility."""
        self._revisions.setdefault(revision.article_id, []).append(revision)
        self._tombstones.discard(revision.article_id)

    def revisions(self, article_id: UUID) -> tuple[ArticleRevision, ...]:
        """Return immutable article revision history."""
        return tuple(self._revisions.get(article_id, []))

    def current_article(self, article_id: UUID) -> ArticleRevision | None:
        """Return current revision unless synchronously tombstoned."""
        if article_id in self._tombstones:
            return None
        revisions = self._revisions.get(article_id, [])
        return revisions[-1] if revisions else None

    def tombstone(self, article_id: UUID) -> None:
        """Synchronously hide all revisions from future reads."""
        self._tombstones.add(article_id)

    def enqueue_job(self, key: EnrichmentJobKey) -> JobClaim:
        """Create one job per five-field identity."""
        existing = self._jobs_by_key.get(key)
        if existing is not None:
            return existing
        job = JobClaim.pending(key)
        self._jobs_by_key[key] = job
        self._jobs_by_id[job.job_id] = job
        return job

    def claim_job(self, job_id: UUID) -> JobClaim:
        """Atomically advance owner generation for a claimed job."""
        job = self._jobs_by_id[job_id]
        claimed = JobClaim(
            job_id=job.job_id, key=job.key, generation=job.generation + 1
        )
        self._jobs_by_id[job_id] = claimed
        self._jobs_by_key[job.key] = claimed
        return claimed

    def complete_job(self, job_id: UUID, generation: int) -> bool:
        """Complete only current fenced owner claim."""
        job = self._jobs_by_id[job_id]
        if job.generation != generation or job.complete:
            return False
        completed = JobClaim(
            job_id=job.job_id,
            key=job.key,
            generation=job.generation,
            complete=True,
        )
        self._jobs_by_id[job_id] = completed
        self._jobs_by_key[job.key] = completed
        return True
