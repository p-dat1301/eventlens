"""Versioned article enrichment contracts."""

# Pydantic custom error literals are intentional contract error codes.
# ruff: noqa: EM101

from enum import StrEnum
from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import (
    ContractModel,
    NonEmptyText,
    NonNegativeInt32,
    PrefixedSha256,
    UtcDatetime,
)
from eventlens_contracts.rag import EvidenceSpan
from eventlens_contracts.taxonomy import TopicTag

BoundedConfidence = Annotated[float, Field(ge=0, le=1)]
ContentType = Literal[
    "news_report",
    "analysis",
    "opinion",
    "press_release",
    "interview",
    "fact_check",
    "live_update",
    "other",
]
EventRelevance = Literal[
    "actionable_event", "background", "follow_up", "no_actionable_event", "uncertain"
]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class EnrichmentStatus(StrEnum):
    """Persisted lifecycle status for one immutable enrichment identity."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"
    STALE = "STALE"


class EnrichedEntity(ContractModel):
    """Evidence-backed canonical entity extraction."""

    entity_id: UUID | None
    entity_type: Literal[
        "LOCATION", "ORG", "PERSON", "SERVICE", "PRODUCT", "FACILITY", "OTHER"
    ]
    canonical_name: NonEmptyText
    surface_forms: tuple[NonEmptyText, ...]
    evidence_span_ids: tuple[UUID, ...]
    confidence: BoundedConfidence

    @model_validator(mode="after")
    def enforce_evidence(self) -> Self:
        """Require unique evidence for extracted entity data."""
        if not self.evidence_span_ids:
            raise _error(
                "empty_entity_evidence", "entities require supporting evidence"
            )
        if len(set(self.evidence_span_ids)) != len(self.evidence_span_ids):
            raise _error(
                "duplicate_entity_evidence", "entity evidence span ids must be unique"
            )
        return self


class EnrichedLocation(ContractModel):
    """Evidence-backed canonical location extraction."""

    location_id: UUID | None
    location_type: Literal["country", "region", "city", "district", "facility", "other"]
    canonical_name: NonEmptyText
    surface_forms: tuple[NonEmptyText, ...]
    evidence_span_ids: tuple[UUID, ...]
    confidence: BoundedConfidence

    @model_validator(mode="after")
    def enforce_evidence(self) -> Self:
        """Require unique evidence for extracted location data."""
        if not self.evidence_span_ids:
            raise _error(
                "empty_location_evidence", "locations require supporting evidence"
            )
        if len(set(self.evidence_span_ids)) != len(self.evidence_span_ids):
            raise _error(
                "duplicate_location_evidence",
                "location evidence span ids must be unique",
            )
        return self


class FieldConfidence(ContractModel):
    """Immutable confidence for one controlled enrichment field."""

    field: Literal["topics", "content_type", "event_relevance", "entities", "locations"]
    confidence: BoundedConfidence


class ArticleEnrichment(ContractModel):
    """Immutable, versioned enrichment result for one article content revision."""

    schema_version: Literal["article_enrichment.v1"]
    article_id: UUID
    content_hash: PrefixedSha256
    enrichment_version: NonEmptyText
    model_version: NonEmptyText
    prompt_version: NonEmptyText
    status: EnrichmentStatus
    topics: tuple[TopicTag, ...]
    content_type: ContentType | None
    event_relevance: EventRelevance | None
    entities: tuple[EnrichedEntity, ...]
    locations: tuple[EnrichedLocation, ...]
    suggested_tags: tuple[NonEmptyText, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    field_confidence: tuple[FieldConfidence, ...]
    enriched_at: UtcDatetime | None
    attempt_count: NonNegativeInt32
    failure_code: NonEmptyText | None

    @model_validator(mode="after")
    def enforce_lifecycle_and_evidence(self) -> Self:  # noqa: C901, PLR0912
        """Validate lifecycle shape and evidence references for immutable output."""
        evidence_ids = tuple(span.span_id for span in self.evidence_spans)
        entity_keys = tuple(
            (entity.entity_id,)
            if entity.entity_id is not None
            else (entity.entity_type, entity.canonical_name)
            for entity in self.entities
        )
        location_keys = tuple(
            (location.location_id,)
            if location.location_id is not None
            else (location.location_type, location.canonical_name)
            for location in self.locations
        )
        confidence_fields = tuple(item.field for item in self.field_confidence)
        if len(set(self.topics)) != len(self.topics):
            raise _error("duplicate_topic", "topics must be unique")
        if len(set(self.suggested_tags)) != len(self.suggested_tags):
            raise _error("duplicate_suggested_tag", "suggested tags must be unique")
        if len(set(entity_keys)) != len(entity_keys):
            raise _error("duplicate_entity", "entities must be unique")
        if len(set(location_keys)) != len(location_keys):
            raise _error("duplicate_location", "locations must be unique")
        if len(set(confidence_fields)) != len(confidence_fields):
            raise _error(
                "duplicate_field_confidence", "field confidence entries must be unique"
            )
        if len(set(evidence_ids)) != len(evidence_ids):
            raise _error(
                "duplicate_enrichment_evidence", "evidence span ids must be unique"
            )
        if any(
            span.article_id != self.article_id or span.content_hash != self.content_hash
            for span in self.evidence_spans
        ):
            raise _error(
                "enrichment_evidence_mismatch",
                "enrichment evidence must match article_id and content_hash",
            )
        referenced_ids = tuple(
            identifier
            for item in (*self.entities, *self.locations)
            for identifier in item.evidence_span_ids
        )
        if any(identifier not in evidence_ids for identifier in referenced_ids):
            raise _error(
                "unknown_enrichment_evidence",
                "extracted fields must reference evidence",
            )
        match self.status:
            case EnrichmentStatus.PENDING:
                if (
                    self.enriched_at is not None
                    or self.failure_code is not None
                    or self.attempt_count != 0
                    or self._has_output()
                ):
                    raise _error(
                        "pending_invalid_metadata",
                        "pending enrichment has no final timestamp or failure code",
                    )
            case EnrichmentStatus.PROCESSING:
                if (
                    self.enriched_at is not None
                    or self.failure_code is not None
                    or self.attempt_count < 1
                    or self._has_output()
                ):
                    raise _error(
                        "processing_invalid_metadata",
                        "processing enrichment requires active attempt only",
                    )
            case EnrichmentStatus.READY:
                if (
                    self.enriched_at is None
                    or self.failure_code is not None
                    or self.attempt_count < 1
                    or not self._has_output()
                ):
                    raise _error(
                        "ready_invalid_metadata",
                        "ready enrichment requires final metadata",
                    )
            case EnrichmentStatus.FAILED:
                if (
                    self.failure_code is None
                    or self.enriched_at is not None
                    or self.attempt_count < 1
                    or self._has_output()
                ):
                    raise _error(
                        "failed_invalid_metadata",
                        "failed enrichment requires failure code",
                    )
            case EnrichmentStatus.STALE:
                if (
                    self.enriched_at is None
                    or self.failure_code is not None
                    or self.attempt_count < 1
                    or not self._has_output()
                ):
                    raise _error(
                        "stale_invalid_metadata",
                        "stale enrichment retains prior final output",
                    )
        return self

    def _has_output(self) -> bool:
        return bool(
            self.topics
            or self.content_type is not None
            or self.event_relevance is not None
            or self.entities
            or self.locations
            or self.suggested_tags
            or self.evidence_spans
            or self.field_confidence
        )
