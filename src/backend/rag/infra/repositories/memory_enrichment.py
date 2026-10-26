"""In-memory enrichment repository and staging for unit tests (never DB evidence).

Mirrors the PostgreSQL predicates with an injected clock so lifecycle, fencing,
retry and cancellation rules can be exercised without a database.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from backend.rag.application.ports.enrichment import Clock
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_lifecycle import (
    CancelAck,
    ClaimedJob,
    EnrichmentIdentity,
    FailureCode,
    IngestReceipt,
    PublishResult,
    RetryPolicy,
    require_transition,
)
from backend.rag.domain.records import EnrichmentJobKey
from backend.rag.domain.staging import StageRequest
from backend.shared.contracts import ArticleEnrichment, EnrichmentStatus

S = EnrichmentStatus


@dataclass(slots=True)
class MemoryJob:  # noqa: MUTABLE_OK
    """Mutable job row."""

    job_id: UUID
    key: EnrichmentJobKey
    status: EnrichmentStatus = S.PENDING
    claim_generation: int = 0
    attempt_count: int = 0
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime | None = None
    failure_code: FailureCode | None = None
    cancel_requested_generation: int | None = None


@dataclass(slots=True)
class InMemoryStaging:  # noqa: MUTABLE_OK
    """GenerationStaging fake keyed exactly like the PostgreSQL identity."""

    generations: dict[tuple[UUID, str, EmbeddingIdentity], int] = field(
        default_factory=dict
    )
    stage_calls: int = 0

    def find_complete(
        self, article: ArticleDocument, identity: EmbeddingIdentity
    ) -> int | None:
        """Return a reusable generation for the exact embedding key."""
        return self.generations.get(
            (article.article_id, article.content_hash, identity)
        )

    def stage(self, request: StageRequest) -> int:
        """Record a complete generation."""
        self.stage_calls += 1
        key = (
            request.article.article_id,
            request.article.content_hash,
            request.identity,
        )
        return self.generations.setdefault(key, len(self.generations) + 1)


@dataclass(slots=True)
class InMemoryEnrichmentRepository:  # noqa: MUTABLE_OK
    """EnrichmentRepository fake with the same fences as PostgreSQL."""

    clock: Clock
    documents: dict[tuple[UUID, str], ArticleDocument] = field(default_factory=dict)
    current: dict[UUID, str] = field(default_factory=dict)
    tombstoned: set[UUID] = field(default_factory=set)
    jobs: dict[UUID, MemoryJob] = field(default_factory=dict)
    publications: list[tuple[UUID, int, int, ArticleEnrichment]] = field(
        default_factory=list
    )
    active_generation: dict[UUID, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock)

    def _set(self, job: MemoryJob, target: EnrichmentStatus) -> None:
        require_transition(job.status, target)
        job.status = target

    def _is_current(self, key: EnrichmentJobKey) -> bool:
        return (
            self.current.get(key.article_id) == key.content_hash
            and key.article_id not in self.tombstoned
        )

    def _by_key(self, key: EnrichmentJobKey) -> MemoryJob | None:
        return next((job for job in self.jobs.values() if job.key == key), None)

    def persist(self, document: ArticleDocument) -> None:
        """Persist a revision only (an article persisted without enqueue)."""
        with self._lock:
            self.calls.append("persist")
            ident = (document.article_id, document.content_hash)
            _ = self.documents.setdefault(ident, document)
            if document.article_id in self.tombstoned:
                return
            if self.current.get(document.article_id) != document.content_hash:
                _ = self.active_generation.pop(document.article_id, None)
            self.current[document.article_id] = document.content_hash
            for job in self.jobs.values():
                if (
                    job.key.article_id == document.article_id
                    and job.key.content_hash != document.content_hash
                    and job.status is not S.STALE
                ):
                    self._set(job, S.STALE)
                    job.lease_expires_at = None

    def tombstone(self, article_id: UUID) -> None:
        """Synchronously hide an article."""
        with self._lock:
            self.tombstoned.add(article_id)
            _ = self.active_generation.pop(article_id, None)

    def _enqueue(self, key: EnrichmentJobKey) -> tuple[UUID, bool]:
        job = self._by_key(key)
        created = job is None
        if job is None:
            job = MemoryJob(uuid4(), key, next_attempt_at=self.clock())
            self.jobs[job.job_id] = job
        elif job.status is S.STALE and self._is_current(key):
            self._set(job, S.PENDING)
            job.attempt_count = 0
            job.failure_code = None
            job.next_attempt_at = self.clock()
            job.cancel_requested_generation = None
        else:
            return job.job_id, False
        for other in self.jobs.values():
            if (
                other.job_id != job.job_id
                and other.key.article_id == key.article_id
                and other.status in (S.PENDING, S.PROCESSING)
            ):
                self._set(other, S.STALE)
                other.lease_expires_at = None
        return job.job_id, created

    def ingest(self, document: ArticleDocument, key: EnrichmentJobKey) -> IngestReceipt:
        """Persist and enqueue atomically."""
        with self._lock:
            self.persist(document)
            if not self._is_current(key):
                return IngestReceipt.not_current(
                    document.article_id, document.content_hash
                )
            job_id, created = self._enqueue(key)
            return IngestReceipt.enqueued(
                document.article_id, document.content_hash, job_id, created=created
            )

    def enqueue_missing(self, identity: EnrichmentIdentity, limit: int) -> int:
        """Reconcile current revisions without a job of this identity."""
        with self._lock:
            self.calls.append("enqueue_missing")
            created = 0
            for article_id, content_hash in sorted(self.current.items()):
                key = identity.key_for(article_id, content_hash)
                if created >= limit or not self._is_current(key):
                    continue
                if self._by_key(key) is None:
                    _ = self._enqueue(key)
                    created += 1
            return created

    def claim_next(
        self, identity: EnrichmentIdentity, policy: RetryPolicy
    ) -> ClaimedJob | None:
        """Sweep then claim the first eligible job of this identity."""
        with self._lock:
            self.calls.append("claim_next")
            now = self.clock()
            versions = (
                identity.enrichment_version,
                identity.model_version,
                identity.prompt_version,
            )
            mine = [
                job
                for job in self.jobs.values()
                if (
                    job.key.enrichment_version,
                    job.key.model_version,
                    job.key.prompt_version,
                )
                == versions
            ]
            for job in mine:
                if job.status in (S.PENDING, S.PROCESSING) and not self._is_current(
                    job.key
                ):
                    self._set(job, S.STALE)
                    job.lease_expires_at = None
                expired = (
                    job.lease_expires_at is not None and job.lease_expires_at <= now
                )
                if (
                    job.status is S.PROCESSING
                    and expired
                    and job.attempt_count >= policy.max_attempts
                ):
                    self._set(job, S.FAILED)
                    job.failure_code = FailureCode.LEASE_EXPIRED
                    job.lease_expires_at = None
            for job in mine:
                pending = (
                    job.status is S.PENDING
                    and job.next_attempt_at is not None
                    and job.next_attempt_at <= now
                )
                expired = (
                    job.status is S.PROCESSING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= now
                )
                if (pending or expired) and job.attempt_count < policy.max_attempts:
                    self._set(job, S.PROCESSING)
                    job.claim_generation += 1
                    job.attempt_count += 1
                    job.lease_expires_at = now + timedelta(seconds=policy.lease_seconds)
                    return ClaimedJob(
                        job.job_id, job.claim_generation, job.attempt_count, job.key
                    )
            return None

    def _owned(self, claim: ClaimedJob) -> MemoryJob | None:
        job = self.jobs.get(claim.job_id)
        if (
            job is None
            or job.claim_generation != claim.claim_generation
            or job.status is not S.PROCESSING
            or job.lease_expires_at is None
            or job.lease_expires_at <= self.clock()
            or job.cancel_requested_generation == claim.claim_generation
        ):
            return None
        return job

    def load_current(self, claim: ClaimedJob) -> ArticleDocument | None:
        """Return the owned current revision."""
        with self._lock:
            self.calls.append("load_current")
            if self._owned(claim) is None or not self._is_current(claim.key):
                return None
            return self.documents[(claim.key.article_id, claim.key.content_hash)]

    def schedule_retry(
        self, claim: ClaimedJob, code: FailureCode, delay_seconds: int
    ) -> bool:
        """Fenced retry schedule."""
        with self._lock:
            self.calls.append("schedule_retry")
            job = self._owned(claim)
            if job is None or not self._is_current(claim.key):
                return False
            self._set(job, S.PENDING)
            job.failure_code = code
            job.lease_expires_at = None
            job.next_attempt_at = self.clock() + timedelta(seconds=delay_seconds)
            return True

    def mark_failed(self, claim: ClaimedJob, code: FailureCode) -> bool:
        """Fenced terminal failure."""
        with self._lock:
            self.calls.append("mark_failed")
            job = self._owned(claim)
            if job is None or not self._is_current(claim.key):
                return False
            self._set(job, S.FAILED)
            job.failure_code = code
            job.lease_expires_at = None
            return True

    def mark_stale(self, claim: ClaimedJob) -> bool:
        """Fenced STALE when no longer current."""
        with self._lock:
            self.calls.append("mark_stale")
            job = self._owned(claim)
            if job is None or self._is_current(claim.key):
                return False
            self._set(job, S.STALE)
            job.lease_expires_at = None
            return True

    def request_cancel(self, claim: ClaimedJob) -> CancelAck:
        """Linearized with publish by the repository lock."""
        with self._lock:
            self.calls.append("request_cancel")
            job = self.jobs.get(claim.job_id)
            if job is None or job.claim_generation != claim.claim_generation:
                return CancelAck.OWNERSHIP_LOST
            if job.status is S.PROCESSING:
                job.cancel_requested_generation = claim.claim_generation
                return CancelAck.ACKNOWLEDGED
            if job.status is S.READY:
                return CancelAck.ALREADY_PUBLISHED
            return CancelAck.OWNERSHIP_LOST

    def publish(
        self, claim: ClaimedJob, index_generation: int, metadata: ArticleEnrichment
    ) -> PublishResult:
        """Fenced atomic READY + metadata + pointer."""
        with self._lock:
            self.calls.append("publish")
            job = self.jobs.get(claim.job_id)
            if (
                job is not None
                and job.cancel_requested_generation == claim.claim_generation
                and job.status is S.PROCESSING
            ):
                return PublishResult.CANCELLED
            key = claim.key
            identity_matches = (
                metadata.article_id,
                metadata.content_hash,
                metadata.enrichment_version,
                metadata.model_version,
                metadata.prompt_version,
            ) == (
                key.article_id,
                key.content_hash,
                key.enrichment_version,
                key.model_version,
                key.prompt_version,
            )
            owned = self._owned(claim)
            if owned is None or not identity_matches or not self._is_current(key):
                return PublishResult.LOST_OWNERSHIP
            self._set(owned, S.READY)
            owned.lease_expires_at = None
            owned.failure_code = None
            for other in self.jobs.values():
                if (
                    other.job_id != owned.job_id
                    and other.key.article_id == key.article_id
                    and other.status is S.READY
                ):
                    self._set(other, S.STALE)
            self.publications.append(
                (owned.job_id, claim.claim_generation, index_generation, metadata)
            )
            self.active_generation[key.article_id] = index_generation
            return PublishResult.PUBLISHED

    def job(self, job_id: UUID) -> MemoryJob:
        """Snapshot one job row for assertions."""
        with self._lock:
            return replace(self.jobs[job_id])
