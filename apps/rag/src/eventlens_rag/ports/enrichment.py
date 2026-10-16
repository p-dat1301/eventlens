"""Enrichment provider and job repository capabilities (framework-free)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.enrichment_draft import EnrichmentDraft, PromptContext
from eventlens_rag.domain.enrichment_lifecycle import (
    CancelAck,
    ClaimedJob,
    EnrichmentIdentity,
    FailureCode,
    IngestReceipt,
    JobOutcome,
    PublishResult,
    RetryPolicy,
)
from eventlens_rag.domain.records import EnrichmentJobKey

type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class EnrichmentRequest:
    """One provider call: allowlisted context, identity and a hard deadline."""

    context: PromptContext
    key: EnrichmentJobKey
    deadline_seconds: int


class EnrichmentProviderError(RuntimeError):
    """Typed provider failure; retry classification belongs to the job policy."""

    def __init__(self, code: FailureCode) -> None:
        """Expose only the stable code, never provider payloads or article text."""
        super().__init__(code.value)
        self.code: FailureCode = code


class ArticleEnricher(Protocol):
    """Produce one structured metadata draft per call, with no internal retry."""

    @property
    def model_version(self) -> str:
        """Model identity recorded in the job key."""
        ...

    @property
    def prompt_version(self) -> str:
        """Prompt identity recorded in the job key."""
        ...

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Return a draft or raise EnrichmentProviderError."""
        ...


class EnrichmentRepository(Protocol):
    """Durable job orchestration: the only owner of budget, schedule and leases."""

    def ingest(self, document: ArticleDocument, key: EnrichmentJobKey) -> IngestReceipt:
        """Persist the article and enqueue its job in one transaction."""
        ...

    def enqueue_missing(self, identity: EnrichmentIdentity, limit: int) -> int:
        """Reconcile current articles lacking a job for this identity."""
        ...

    def claim_next(
        self, identity: EnrichmentIdentity, policy: RetryPolicy
    ) -> ClaimedJob | None:
        """Claim one eligible job of this identity, advancing its fence."""
        ...

    def load_current(self, claim: ClaimedJob) -> ArticleDocument | None:
        """Return the claimed revision only while it is current and visible."""
        ...

    def schedule_retry(
        self, claim: ClaimedJob, code: FailureCode, delay_seconds: int
    ) -> bool:
        """Fenced PROCESSING -> PENDING with DB-clock eligibility time."""
        ...

    def mark_failed(self, claim: ClaimedJob, code: FailureCode) -> bool:
        """Fenced PROCESSING -> FAILED."""
        ...

    def mark_stale(self, claim: ClaimedJob) -> bool:
        """Fenced PROCESSING -> STALE when the revision is no longer current."""
        ...

    def request_cancel(self, claim: ClaimedJob) -> CancelAck:
        """Record cancellation, serialized against publication by a row lock."""
        ...

    def publish(
        self, claim: ClaimedJob, index_generation: int, metadata: ArticleEnrichment
    ) -> PublishResult:
        """Commit READY, metadata and active index pointer together, fenced."""
        ...


class TelemetrySink(Protocol):
    """Receive per-job outcomes that never contain article text."""

    def emit(self, outcome: JobOutcome) -> None:
        """Record one outcome."""
        ...
