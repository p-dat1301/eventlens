"""Enrichment job identity, lifecycle transitions and the versioned retry policy."""

# ruff: noqa: EM101

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Annotated, Final, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from backend.rag.domain.records import EnrichmentJobKey
from backend.shared.contracts import EnrichmentStatus
from backend.shared.contracts.base import ContractModel, NonEmptyText

PositiveInt = Annotated[int, Field(ge=1)]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


@dataclass(frozen=True, slots=True)
class EnrichmentIdentity:
    """Versions a worker produces; with article/hash it forms the 5-field key."""

    enrichment_version: str
    model_version: str
    prompt_version: str

    def key_for(self, article_id: UUID, content_hash: str) -> EnrichmentJobKey:
        """Return the idempotency key for one article revision."""
        return EnrichmentJobKey(
            article_id=article_id,
            content_hash=content_hash,
            enrichment_version=self.enrichment_version,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
        )


@unique
class FailureCode(StrEnum):
    """Stable typed enrichment failure codes."""

    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_REFUSAL = "provider_refusal"
    MALFORMED_OUTPUT = "malformed_output"
    UNSUPPORTED_TAXONOMY = "unsupported_taxonomy"
    UNGROUNDED_EVIDENCE = "ungrounded_evidence"
    UNGROUNDED_METADATA = "ungrounded_metadata"
    INVALID_METADATA = "invalid_metadata"
    INVALID_STAGE = "invalid_stage"
    LEASE_EXPIRED = "lease_expired"


class IllegalTransitionError(ValueError):
    """A lifecycle write would move a job along a forbidden edge."""

    def __init__(self, source: EnrichmentStatus, target: EnrichmentStatus) -> None:
        """Name both ends of the rejected edge."""
        super().__init__(f"illegal enrichment transition {source} -> {target}")


S = EnrichmentStatus
LEGAL_TRANSITIONS: Final[frozenset[tuple[EnrichmentStatus, EnrichmentStatus]]] = (
    frozenset(
        {
            (S.PENDING, S.PROCESSING),  # claim
            (S.PENDING, S.STALE),  # correction / tombstone / supersession
            (S.PROCESSING, S.PROCESSING),  # lease-expiry reclaim (new fence)
            (S.PROCESSING, S.READY),  # fenced publication
            (S.PROCESSING, S.PENDING),  # retryable failure, scheduled backoff
            (S.PROCESSING, S.FAILED),  # non-retryable or budget exhausted
            (S.PROCESSING, S.STALE),  # article/version changed mid-flight
            (S.READY, S.STALE),  # newer identity published / correction
            (S.FAILED, S.STALE),  # correction after terminal failure
            (S.STALE, S.PENDING),  # same revision current again (A-B-A)
        }
    )
)


def require_transition(source: EnrichmentStatus, target: EnrichmentStatus) -> None:
    """Reject every edge outside the documented lifecycle."""
    if (source, target) not in LEGAL_TRANSITIONS:
        raise IllegalTransitionError(source, target)


class RetryPolicy(ContractModel):
    """Versioned attempt budget, deadline, lease and backoff policy.

    Attempts are counted per claim, including claims whose lease expired, so a
    crashed worker consumes budget exactly like a failed call. No jitter: the
    schedule is deterministic and auditable.
    """

    schema_version: Literal["enrichment_retry_policy.v1"]
    policy_id: NonEmptyText
    max_attempts: PositiveInt
    attempt_timeout_seconds: PositiveInt
    lease_seconds: PositiveInt
    backoff_initial_seconds: PositiveInt
    backoff_multiplier: PositiveInt
    backoff_max_seconds: PositiveInt
    max_jobs_per_pass: PositiveInt
    retryable_failure_codes: tuple[FailureCode, ...]

    @model_validator(mode="after")
    def enforce_safe_lease(self) -> Self:
        """Lease must outlive one provider deadline plus staging/publication."""
        if self.lease_seconds < 2 * self.attempt_timeout_seconds:
            raise _error(
                "lease_shorter_than_deadline",
                "lease_seconds must be at least twice attempt_timeout_seconds",
            )
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            raise _error(
                "invalid_backoff_cap", "backoff_max_seconds below initial backoff"
            )
        return self

    def backoff_seconds(self, failed_attempt: int) -> int:
        """Delay before the job becomes claimable again after this attempt."""
        delay = self.backoff_initial_seconds
        for _ in range(max(failed_attempt - 1, 0)):
            delay = min(delay * self.backoff_multiplier, self.backoff_max_seconds)
        return min(delay, self.backoff_max_seconds)

    def decide(self, failed_attempt: int, code: FailureCode) -> "RetryDecision":
        """Retry only retryable codes inside the attempt budget."""
        if code not in self.retryable_failure_codes:
            return RetryDecision(code, retry_in_seconds=None, exhausted=False)
        if failed_attempt >= self.max_attempts:
            return RetryDecision(code, retry_in_seconds=None, exhausted=True)
        return RetryDecision(
            code, retry_in_seconds=self.backoff_seconds(failed_attempt), exhausted=False
        )


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Retry schedule (seconds, DB clock) or terminal failure."""

    code: FailureCode
    retry_in_seconds: int | None
    exhausted: bool


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Fenced ownership of one job: valid only while generation and lease hold."""

    job_id: UUID
    claim_generation: int
    attempt: int
    key: EnrichmentJobKey


@unique
class OutcomeKind(StrEnum):
    """Observable result of one job invocation."""

    READY = "READY"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    STALE = "STALE"
    LOST_OWNERSHIP = "LOST_OWNERSHIP"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Telemetry-safe outcome: identifiers, codes and counts, never article text."""

    job_id: UUID
    article_id: UUID
    claim_generation: int
    attempt: int
    kind: OutcomeKind
    failure_code: FailureCode | None = None
    retry_in_seconds: int | None = None
    index_generation: int | None = None
    embedding_reused: bool | None = None

    def as_record(self) -> dict[str, str | int | bool | None]:
        """Flat JSON-safe record for telemetry sinks and CLI output."""
        return {
            "job_id": str(self.job_id),
            "article_id": str(self.article_id),
            "claim_generation": self.claim_generation,
            "attempt": self.attempt,
            "outcome": self.kind.value,
            "failure_code": None if self.failure_code is None else self.failure_code,
            "retry_in_seconds": self.retry_in_seconds,
            "index_generation": self.index_generation,
            "embedding_reused": self.embedding_reused,
        }


@unique
class PublishResult(StrEnum):
    """Result of the fenced publication transaction."""

    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    LOST_OWNERSHIP = "LOST_OWNERSHIP"


@unique
class CancelAck(StrEnum):
    """Linearized cancellation result against the publication boundary."""

    ACKNOWLEDGED = "ACKNOWLEDGED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    OWNERSHIP_LOST = "OWNERSHIP_LOST"


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    """Durable ingest result: article persisted and, when current, job enqueued."""

    article_id: UUID
    content_hash: str
    job_id: UUID | None
    job_created: bool
    current: bool

    @classmethod
    def not_current(cls, article_id: UUID, content_hash: str) -> "IngestReceipt":
        """Revision persisted but hidden (tombstoned); no job enqueued."""
        return cls(article_id, content_hash, None, job_created=False, current=False)

    @classmethod
    def enqueued(
        cls, article_id: UUID, content_hash: str, job_id: UUID, *, created: bool
    ) -> "IngestReceipt":
        """Revision current and its job durable (new or existing)."""
        return cls(article_id, content_hash, job_id, job_created=created, current=True)
