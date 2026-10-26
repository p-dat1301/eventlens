"""Typed values shared across worker application and adapters."""

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Annotated, ClassVar, Literal, Self, assert_never
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts import Event

PROCESSING_TRACE_ACCEPT_REASONS = "processing_trace_accept_reasons"
PROCESSING_TRACE_ACCEPT_WRITE = "processing_trace_accept_write"
PROCESSING_TRACE_REJECT_REASON_REQUIRED = "processing_trace_reject_reason_required"
PROCESSING_TRACE_REASONS_NON_CANONICAL = "processing_trace_reasons_non_canonical"
PROCESSING_TRACE_REASONS_REQUIRED = "processing_trace_reasons_required"
PROCESSING_TRACE_READY_PROHIBITED = "processing_trace_ready_prohibited"
PROCESSING_TRACE_REVIEW_REJECT_REASON = "processing_trace_review_reject_reason"
PROCESSING_TRACE_WRITE_PROHIBITED = "processing_trace_write_prohibited"
PROPOSAL_EVENT_CONFIDENCE_REQUIRED = "proposal_event_confidence_required"


def _require_non_blank(value: str) -> str:
    if not value.strip():
        message = "text must not be blank"
        raise ValueError(message)
    return value


NonEmptyText = Annotated[
    str, Field(min_length=1, pattern=r"\S"), AfterValidator(_require_non_blank)
]


class WorkerModel(BaseModel):
    """Strict immutable base for worker-local boundary models."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, strict=True
    )


@unique
class GateVerdict(StrEnum):
    """Closed set of proposal gate outcomes."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REVIEW = "REVIEW"


@unique
class GateReason(StrEnum):
    """Closed set of reasons supporting a gate verdict."""

    ARTICLE_ID_MISMATCH = "article_id_mismatch"
    DUPLICATE_ARTICLE = "duplicate_article"
    LOW_CONFIDENCE = "low_confidence"
    FLAGGED_INPUT = "flagged_input"
    MISSING_EVENT_TIME = "missing_event_time"
    READY = "ready"


CANONICAL_REASON_ORDER = tuple(GateReason)


@unique
class WriteDisposition(StrEnum):
    """Observable outcomes from idempotent event persistence."""

    INSERTED = "inserted"
    UNCHANGED = "unchanged"


class EventProposal(WorkerModel):
    """Validated extraction result crossing an adapter boundary."""

    proposal_id: UUID
    article_id: UUID
    extractor_version: NonEmptyText
    event: Event

    @model_validator(mode="after")
    def require_event_confidence(self) -> Self:
        """Require confidence on proposed event before gate evaluation."""
        if self.event.confidence is None:
            message = "proposed event requires confidence"
            raise PydanticCustomError(PROPOSAL_EVENT_CONFIDENCE_REQUIRED, message)
        return self


class ProcessingTrace(WorkerModel):
    """Worker-local outcome; event_id is proposed ID when event was not persisted."""

    schema_version: Literal["processing_trace.v1"]
    article_id: UUID
    proposal_id: UUID
    event_id: UUID
    verdict: GateVerdict
    reasons: tuple[GateReason, ...]
    write_disposition: WriteDisposition | None = None

    @model_validator(mode="after")
    def enforce_verdict_outcome(self) -> Self:
        """Reject impossible gate and persistence outcome combinations."""
        match self.verdict:
            case GateVerdict.ACCEPT:
                return self._enforce_accept()
            case GateVerdict.REJECT:
                return self._enforce_reject()
            case GateVerdict.REVIEW:
                return self._enforce_review()
        assert_never(self.verdict)

    def _enforce_accept(self) -> Self:
        if self.reasons != (GateReason.READY,):
            message = "accepted trace requires ready as its only reason"
            raise PydanticCustomError(
                PROCESSING_TRACE_ACCEPT_REASONS,
                message,
            )
        if self.write_disposition is None:
            message = "accepted trace requires write disposition"
            raise PydanticCustomError(PROCESSING_TRACE_ACCEPT_WRITE, message)
        return self

    def _enforce_non_accept(self) -> None:
        if not self.reasons:
            message = "rejected or reviewed trace requires reasons"
            raise PydanticCustomError(
                PROCESSING_TRACE_REASONS_REQUIRED,
                message,
            )
        if GateReason.READY in self.reasons:
            message = "rejected or reviewed trace prohibits ready reason"
            raise PydanticCustomError(
                PROCESSING_TRACE_READY_PROHIBITED,
                message,
            )
        canonical_reasons = tuple(
            reason for reason in CANONICAL_REASON_ORDER if reason in self.reasons
        )
        if self.reasons != canonical_reasons:
            message = "trace reasons must be unique and canonically ordered"
            raise PydanticCustomError(
                PROCESSING_TRACE_REASONS_NON_CANONICAL,
                message,
            )
        if self.write_disposition is not None:
            message = "rejected or reviewed trace prohibits write disposition"
            raise PydanticCustomError(
                PROCESSING_TRACE_WRITE_PROHIBITED,
                message,
            )

    def _enforce_reject(self) -> Self:
        self._enforce_non_accept()
        if not any(
            reason
            in (
                GateReason.ARTICLE_ID_MISMATCH,
                GateReason.DUPLICATE_ARTICLE,
                GateReason.LOW_CONFIDENCE,
            )
            for reason in self.reasons
        ):
            message = "rejected trace requires at least one reject reason"
            raise PydanticCustomError(
                PROCESSING_TRACE_REJECT_REASON_REQUIRED,
                message,
            )
        return self

    def _enforce_review(self) -> Self:
        self._enforce_non_accept()
        if any(
            reason
            in (
                GateReason.ARTICLE_ID_MISMATCH,
                GateReason.DUPLICATE_ARTICLE,
                GateReason.LOW_CONFIDENCE,
            )
            for reason in self.reasons
        ):
            message = "reviewed trace prohibits reject reasons"
            raise PydanticCustomError(
                PROCESSING_TRACE_REVIEW_REJECT_REASON,
                message,
            )
        return self


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Internal gate outcome for one proposal."""

    verdict: GateVerdict
    reasons: tuple[GateReason, ...]


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Internal persistence outcome for one event."""

    disposition: WriteDisposition
