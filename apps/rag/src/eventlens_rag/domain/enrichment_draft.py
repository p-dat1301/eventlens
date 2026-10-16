"""Provider-facing prompt context and structural validation of enrichment drafts.

The provider proposes only metadata. Job identity, lifecycle status, attempt count,
timestamps, span IDs and fingerprints are derived here from application-owned
values; provider-declared values for those fields are not part of the draft schema
and are rejected as malformed output. Validation is structural and linkage-only
(taxonomy, bounds, offsets, excerpts, fingerprints); semantic entailment is T9.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Final, get_args
from uuid import UUID, uuid5

from pydantic import Field, ValidationError

from eventlens_contracts import ArticleEnrichment, EnrichmentStatus, EvidenceSpan
from eventlens_contracts.base import ContractModel, NonEmptyText, NonNegativeInt32
from eventlens_contracts.enrichment import (
    ContentType,
    EnrichedEntity,
    EnrichedLocation,
    EventRelevance,
    FieldConfidence,
)
from eventlens_contracts.taxonomy import TopicTag
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.enrichment_lifecycle import (
    ClaimedJob,
    FailureCode,
)

SPAN_NAMESPACE: Final = UUID("6c1f0f0e-7f5b-4c33-9a57-8d1f3c2b9e41")
MAX_SUGGESTED_TAGS: Final = 10
BoundedConfidence = Annotated[float, Field(ge=0, le=1)]

TOPICS: Final = frozenset(get_args(TopicTag))
CONTENT_TYPES: Final = frozenset(get_args(ContentType))
RELEVANCE: Final = frozenset(get_args(EventRelevance))
ENTITY_TYPES: Final = frozenset(
    ("LOCATION", "ORG", "PERSON", "SERVICE", "PRODUCT", "FACILITY", "OTHER")
)
LOCATION_TYPES: Final = frozenset(
    ("country", "region", "city", "district", "facility", "other")
)
CONFIDENCE_FIELDS: Final = frozenset(
    ("topics", "content_type", "event_relevance", "entities", "locations")
)

# Allowlist of article fields that may cross the provider boundary.
PROMPT_FIELDS: Final = (
    "article_id",
    "title",
    "normalized_content",
    "source_name",
    "source_domain",
    "source_published_at",
)


class PromptContext(ContractModel):
    """Allowlisted article fields only; everything else is redacted by omission."""

    article_id: UUID
    title: NonEmptyText
    normalized_content: NonEmptyText
    source_name: NonEmptyText
    source_domain: NonEmptyText
    source_published_at: str | None

    @classmethod
    def from_document(cls, document: ArticleDocument) -> "PromptContext":
        """Copy only allowlisted fields; URLs, IDs and raw content never leave."""
        published = document.source_published_at
        return cls(
            article_id=document.article_id,
            title=document.title,
            normalized_content=document.normalized_content,
            source_name=document.source_name,
            source_domain=document.source_domain,
            source_published_at=None if published is None else published.isoformat(),
        )


class DraftEvidence(ContractModel):
    """Proposed code-point half-open range with its exact excerpt."""

    start_offset: NonNegativeInt32
    end_offset: NonNegativeInt32
    excerpt: NonEmptyText


class DraftEntity(ContractModel):
    """Proposed entity; canonical IDs are not accepted from the provider."""

    entity_type: NonEmptyText
    canonical_name: NonEmptyText
    surface_forms: tuple[NonEmptyText, ...]
    evidence_refs: tuple[NonNegativeInt32, ...]
    confidence: BoundedConfidence


class DraftLocation(ContractModel):
    """Proposed location; canonical IDs are not accepted from the provider."""

    location_type: NonEmptyText
    canonical_name: NonEmptyText
    surface_forms: tuple[NonEmptyText, ...]
    evidence_refs: tuple[NonNegativeInt32, ...]
    confidence: BoundedConfidence


class DraftFieldConfidence(ContractModel):
    """Proposed confidence for one controlled field."""

    field: NonEmptyText
    confidence: BoundedConfidence


class EnrichmentDraft(ContractModel):
    """Structured provider output schema (metadata proposal only)."""

    topics: tuple[NonEmptyText, ...]
    content_type: NonEmptyText | None
    event_relevance: NonEmptyText | None
    entities: tuple[DraftEntity, ...]
    locations: tuple[DraftLocation, ...]
    suggested_tags: tuple[NonEmptyText, ...]
    evidence: tuple[DraftEvidence, ...]
    field_confidence: tuple[DraftFieldConfidence, ...]


class DraftRejectedError(ValueError):
    """Draft failed structural validation; never retried blindly."""

    def __init__(self, code: FailureCode, detail: str) -> None:
        """Keep a stable code plus a short non-content diagnostic."""
        super().__init__(f"{code}: {detail}")
        self.code: FailureCode = code


@dataclass(frozen=True, slots=True)
class ValidatedEnrichment:
    """READY metadata bound to one claimed job identity."""

    metadata: ArticleEnrichment


def _span(article: ArticleDocument, draft: DraftEvidence) -> EvidenceSpan:
    content = article.normalized_content
    if (
        draft.end_offset <= draft.start_offset
        or draft.end_offset > len(content)
        or content[draft.start_offset : draft.end_offset] != draft.excerpt
    ):
        raise DraftRejectedError(
            FailureCode.UNGROUNDED_EVIDENCE,
            f"span {draft.start_offset}:{draft.end_offset} does not match content",
        )
    identity = (
        article.article_id,
        article.content_hash,
        draft.start_offset,
        draft.end_offset,
    )
    span_id = uuid5(SPAN_NAMESPACE, ":".join(map(str, identity)))
    return EvidenceSpan(
        span_id=span_id,
        article_id=article.article_id,
        content_hash=article.content_hash,
        start_offset=draft.start_offset,
        end_offset=draft.end_offset,
        excerpt=draft.excerpt,
        span_fingerprint=f"sha256:{sha256(draft.excerpt.encode()).hexdigest()}",
        offset_basis="unicode_codepoint",
    )


def _check_taxonomy(draft: EnrichmentDraft) -> None:
    unsupported = (
        [topic for topic in draft.topics if topic not in TOPICS]
        + [
            value
            for value in (draft.content_type,)
            if value is not None and value not in CONTENT_TYPES
        ]
        + [
            value
            for value in (draft.event_relevance,)
            if value is not None and value not in RELEVANCE
        ]
        + [e.entity_type for e in draft.entities if e.entity_type not in ENTITY_TYPES]
        + [
            loc.location_type
            for loc in draft.locations
            if loc.location_type not in LOCATION_TYPES
        ]
        + [f.field for f in draft.field_confidence if f.field not in CONFIDENCE_FIELDS]
    )
    if unsupported:
        raise DraftRejectedError(
            FailureCode.UNSUPPORTED_TAXONOMY, f"{len(unsupported)} unsupported values"
        )


def _grounded_refs(
    refs: tuple[int, ...],
    surface_forms: tuple[str, ...],
    spans: tuple[EvidenceSpan, ...],
) -> tuple[UUID, ...]:
    """Every surface form must occur inside one of its cited excerpts."""
    if not refs or not surface_forms or any(ref >= len(spans) for ref in refs):
        raise DraftRejectedError(
            FailureCode.UNGROUNDED_EVIDENCE, "missing or unknown evidence reference"
        )
    cited = tuple(spans[ref] for ref in refs)
    for form in surface_forms:
        if not any(form in span.excerpt for span in cited):
            raise DraftRejectedError(
                FailureCode.UNGROUNDED_METADATA, "surface form absent from evidence"
            )
    return tuple(dict.fromkeys(span.span_id for span in cited))


def _suggested(tags: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize out-of-taxonomy suggestions; they never become primary topics."""
    cleaned = (tag.strip().casefold() for tag in tags)
    unique = tuple(dict.fromkeys(tag for tag in cleaned if tag))
    return unique[:MAX_SUGGESTED_TAGS]


def validate_draft(
    article: ArticleDocument,
    claim: ClaimedJob,
    draft: EnrichmentDraft,
    enriched_at: datetime,
) -> ValidatedEnrichment:
    """Bind a draft to the claimed identity and the exact normalized content."""
    key = claim.key
    if key.article_id != article.article_id or key.content_hash != article.content_hash:
        raise DraftRejectedError(
            FailureCode.INVALID_METADATA, "claim does not match current article"
        )
    _check_taxonomy(draft)
    spans = tuple(_span(article, item) for item in draft.evidence)
    try:
        metadata = _build(claim, draft, spans, enriched_at)
    except ValidationError as error:
        codes = sorted({str(item["type"]) for item in error.errors()})
        raise DraftRejectedError(
            FailureCode.INVALID_METADATA, ",".join(codes)
        ) from error
    return ValidatedEnrichment(metadata)


def _build(
    claim: ClaimedJob,
    draft: EnrichmentDraft,
    spans: tuple[EvidenceSpan, ...],
    enriched_at: datetime,
) -> ArticleEnrichment:
    key = claim.key
    entities = tuple(
        EnrichedEntity.model_validate(
            {
                "entity_id": None,
                "entity_type": entity.entity_type,
                "canonical_name": entity.canonical_name,
                "surface_forms": entity.surface_forms,
                "evidence_span_ids": _grounded_refs(
                    entity.evidence_refs, entity.surface_forms, spans
                ),
                "confidence": entity.confidence,
            },
            strict=False,
        )
        for entity in draft.entities
    )
    locations = tuple(
        EnrichedLocation.model_validate(
            {
                "location_id": None,
                "location_type": location.location_type,
                "canonical_name": location.canonical_name,
                "surface_forms": location.surface_forms,
                "evidence_span_ids": _grounded_refs(
                    location.evidence_refs, location.surface_forms, spans
                ),
                "confidence": location.confidence,
            },
            strict=False,
        )
        for location in draft.locations
    )
    return ArticleEnrichment.model_validate(
        {
            "schema_version": "article_enrichment.v1",
            "article_id": key.article_id,
            "content_hash": key.content_hash,
            "enrichment_version": key.enrichment_version,
            "model_version": key.model_version,
            "prompt_version": key.prompt_version,
            "status": EnrichmentStatus.READY,
            "topics": draft.topics,
            "content_type": draft.content_type,
            "event_relevance": draft.event_relevance,
            "entities": entities,
            "locations": locations,
            "suggested_tags": _suggested(draft.suggested_tags),
            "evidence_spans": tuple(dict.fromkeys(spans)),
            "field_confidence": tuple(
                FieldConfidence.model_validate(
                    {"field": item.field, "confidence": item.confidence},
                    strict=False,
                )
                for item in draft.field_confidence
            ),
            "enriched_at": enriched_at,
            "attempt_count": claim.attempt,
            "failure_code": None,
        },
        strict=False,
    )
