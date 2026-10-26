from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.shared.contracts import (
    ArticleEnrichment,
    EnrichedEntity,
    EnrichedLocation,
    EnrichmentStatus,
    EvidenceSpan,
    FieldConfidence,
)

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
HASH = "sha256:" + "a" * 64


def enrichment() -> ArticleEnrichment:
    return ArticleEnrichment.model_validate(
        {
            "schema_version": "article_enrichment.v1",
            "article_id": ARTICLE_ID,
            "content_hash": HASH,
            "enrichment_version": "enrichment-1",
            "model_version": "model-1",
            "prompt_version": "prompt-1",
            "status": EnrichmentStatus.READY,
            "topics": ("technology",),
            "content_type": "news_report",
            "event_relevance": "actionable_event",
            "entities": (),
            "locations": (),
            "suggested_tags": (),
            "evidence_spans": (),
            "field_confidence": (),
            "enriched_at": datetime(2026, 9, 23, 10, 0, tzinfo=UTC),
            "attempt_count": 1,
            "failure_code": None,
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "article_enrichment.v2"),
        ("content_hash", "sha256:" + "Z" * 64),
        ("enrichment_version", " "),
        ("model_version", " "),
        ("prompt_version", " "),
    ],
)
def test_enrichment_rejects_invalid_revision_identity(field: str, value: str) -> None:
    payload = enrichment().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_field_confidence_rejects_out_of_bounds(confidence: float) -> None:
    with pytest.raises(ValidationError, match="confidence"):
        _ = FieldConfidence(field="topics", confidence=confidence)


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 9, 23, 10, 0, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 9, 23, 17, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_enrichment_requires_utc_final_timestamp(timestamp: datetime) -> None:
    payload = enrichment().model_dump()
    payload["enriched_at"] = timestamp
    with pytest.raises(ValidationError, match="enriched_at"):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize("kind", ["entity", "location"])
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [("confidence", -0.1, "confidence"), ("evidence_span_ids", (), "empty_")],
)
def test_extracted_values_reject_invalid_confidence_or_evidence(
    kind: str, field: str, value: float | tuple[UUID, ...], error: str
) -> None:
    models = {
        "entity": (EnrichedEntity, {"entity_id": ARTICLE_ID, "entity_type": "ORG"}),
        "location": (
            EnrichedLocation,
            {"location_id": ARTICLE_ID, "location_type": "city"},
        ),
    }
    model, identity = models[kind]
    payload = {
        **identity,
        "canonical_name": "Example",
        "surface_forms": ("Example",),
        "evidence_span_ids": (ARTICLE_ID,),
        "confidence": 0.5,
    }
    with pytest.raises(ValidationError, match=error):
        _ = model.model_validate({**payload, field: value})


def test_enrichment_rejects_unlinked_extracted_evidence() -> None:
    payload = enrichment().model_dump()
    payload["entities"] = (
        {
            "entity_id": ARTICLE_ID,
            "entity_type": "ORG",
            "canonical_name": "Example",
            "surface_forms": ("Example",),
            "evidence_span_ids": (ARTICLE_ID,),
            "confidence": 0.5,
        },
    )
    with pytest.raises(ValidationError, match="unknown_enrichment_evidence"):
        _ = ArticleEnrichment.model_validate(payload)


def test_enrichment_accepts_extracted_entity_linked_to_matching_span() -> None:
    span = EvidenceSpan(
        span_id=ARTICLE_ID,
        article_id=ARTICLE_ID,
        content_hash=HASH,
        start_offset=0,
        end_offset=1,
        excerpt="x",
        span_fingerprint="sha256:" + sha256(b"x").hexdigest(),
        offset_basis="unicode_codepoint",
    )
    payload = enrichment().model_dump()
    payload["evidence_spans"] = (span.model_dump(),)
    payload["entities"] = (
        {
            "entity_id": ARTICLE_ID,
            "entity_type": "ORG",
            "canonical_name": "Example",
            "surface_forms": ("Example",),
            "evidence_span_ids": (span.span_id,),
            "confidence": 0.0,
        },
    )
    assert ArticleEnrichment.model_validate(payload).entities[0].evidence_span_ids == (
        span.span_id,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("article_id", UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")),
        ("content_hash", "sha256:" + "b" * 64),
    ],
)
def test_enrichment_rejects_evidence_with_wrong_article_revision(
    field: str, value: UUID | str
) -> None:
    span = EvidenceSpan(
        span_id=ARTICLE_ID,
        article_id=ARTICLE_ID,
        content_hash=HASH,
        start_offset=0,
        end_offset=1,
        excerpt="x",
        span_fingerprint="sha256:" + sha256(b"x").hexdigest(),
        offset_basis="unicode_codepoint",
    )
    payload = enrichment().model_dump()
    payload["evidence_spans"] = ({**span.model_dump(), field: value},)
    with pytest.raises(ValidationError, match="enrichment_evidence_mismatch"):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "error"),
    [("entities", "duplicate_entity"), ("locations", "duplicate_location")],
)
def test_enrichment_rejects_duplicate_extracted_identity(
    field: str, error: str
) -> None:
    span = EvidenceSpan(
        span_id=ARTICLE_ID,
        article_id=ARTICLE_ID,
        content_hash=HASH,
        start_offset=0,
        end_offset=1,
        excerpt="x",
        span_fingerprint="sha256:" + sha256(b"x").hexdigest(),
        offset_basis="unicode_codepoint",
    )
    payload = enrichment().model_dump()
    payload["evidence_spans"] = (span.model_dump(),)
    identity = (
        {"entity_id": ARTICLE_ID, "entity_type": "ORG"}
        if field == "entities"
        else {"location_id": ARTICLE_ID, "location_type": "city"}
    )
    item = {
        **identity,
        "canonical_name": "Example",
        "surface_forms": ("Example",),
        "evidence_span_ids": (ARTICLE_ID,),
        "confidence": 0.5,
    }
    payload[field] = (item, item)
    with pytest.raises(ValidationError, match=error):
        _ = ArticleEnrichment.model_validate(payload)


def test_enrichment_rejects_duplicate_span_id() -> None:
    span = EvidenceSpan(
        span_id=ARTICLE_ID,
        article_id=ARTICLE_ID,
        content_hash=HASH,
        start_offset=0,
        end_offset=1,
        excerpt="x",
        span_fingerprint="sha256:" + sha256(b"x").hexdigest(),
        offset_basis="unicode_codepoint",
    )
    payload = enrichment().model_dump()
    payload["evidence_spans"] = (span.model_dump(), span.model_dump())
    with pytest.raises(ValidationError, match="duplicate_enrichment_evidence"):
        _ = ArticleEnrichment.model_validate(payload)
