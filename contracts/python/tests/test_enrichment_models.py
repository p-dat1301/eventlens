from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens_contracts import ArticleEnrichment, EnrichmentStatus, FieldConfidence

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
HASH = "sha256:" + "a" * 64
UTC_VALUE = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def enrichment(status: EnrichmentStatus) -> ArticleEnrichment:
    payload: dict[str, object] = {
        "schema_version": "article_enrichment.v1",
        "article_id": ARTICLE_ID,
        "content_hash": HASH,
        "enrichment_version": "enrichment-1",
        "model_version": "model-1",
        "prompt_version": "prompt-1",
        "status": status,
        "topics": ()
        if status
        in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }
        else ("technology",),
        "content_type": None
        if status
        in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }
        else "news_report",
        "event_relevance": None
        if status
        in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }
        else "actionable_event",
        "entities": (),
        "locations": (),
        "suggested_tags": ()
        if status
        in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }
        else ("unlisted-discovery-tag",),
        "evidence_spans": (),
        "field_confidence": ()
        if status
        in {
            EnrichmentStatus.PENDING,
            EnrichmentStatus.PROCESSING,
            EnrichmentStatus.FAILED,
        }
        else (FieldConfidence(field="topics", confidence=1.0),),
        "enriched_at": UTC_VALUE
        if status in {EnrichmentStatus.READY, EnrichmentStatus.STALE}
        else None,
        "attempt_count": 1 if status is not EnrichmentStatus.PENDING else 0,
        "failure_code": "provider_timeout"
        if status is EnrichmentStatus.FAILED
        else None,
    }
    return ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize("status", tuple(EnrichmentStatus))
def test_lifecycle_valid_states(status: EnrichmentStatus) -> None:
    assert enrichment(status).status is status


def test_suggested_tag_can_be_outside_controlled_taxonomy() -> None:
    assert enrichment(EnrichmentStatus.READY).suggested_tags == (
        "unlisted-discovery-tag",
    )


@pytest.mark.parametrize("status", [EnrichmentStatus.READY, EnrichmentStatus.FAILED])
def test_lifecycle_rejects_missing_failure_or_final_metadata(
    status: EnrichmentStatus,
) -> None:
    payload = enrichment(status).model_dump()
    if status is EnrichmentStatus.READY:
        payload["enriched_at"] = None
    else:
        payload["failure_code"] = None
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topics", ("unsupported",)),
        ("field_confidence", ({"field": "topics", "confidence": 1.1},)),
    ],
)
def test_taxonomy_and_confidence_reject_invalid_values(
    field: str, value: tuple[str] | tuple[dict[str, float | str], ...]
) -> None:
    payload = enrichment(EnrichmentStatus.READY).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


def test_enrichment_is_frozen_and_extra_forbidden() -> None:
    result = enrichment(EnrichmentStatus.READY)
    with pytest.raises(ValidationError):
        result.status = EnrichmentStatus.FAILED
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate({**result.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        result.field_confidence[0].confidence = 0.2


@pytest.mark.parametrize(
    ("status", "field", "value"),
    [
        (EnrichmentStatus.PENDING, "attempt_count", 1),
        (EnrichmentStatus.PENDING, "topics", ("technology",)),
        (EnrichmentStatus.PROCESSING, "suggested_tags", ("tag",)),
        (EnrichmentStatus.FAILED, "enriched_at", UTC_VALUE),
        (
            EnrichmentStatus.FAILED,
            "field_confidence",
            ({"field": "topics", "confidence": 1.0},),
        ),
    ],
)
def test_lifecycle_rejects_output_or_invalid_metadata(
    status: EnrichmentStatus,
    field: str,
    value: int | datetime | tuple[str] | tuple[dict[str, float | str], ...],
) -> None:
    payload = enrichment(status).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize("status", [EnrichmentStatus.READY, EnrichmentStatus.STALE])
def test_completed_lifecycle_rejects_empty_output(status: EnrichmentStatus) -> None:
    payload = enrichment(status).model_dump()
    payload.update(
        {
            "topics": (),
            "content_type": None,
            "event_relevance": None,
            "entities": (),
            "locations": (),
            "suggested_tags": (),
            "evidence_spans": (),
            "field_confidence": (),
        }
    )
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topics", ("technology", "technology")),
        ("suggested_tags", ("tag", "tag")),
        (
            "field_confidence",
            (
                {"field": "topics", "confidence": 1.0},
                {"field": "topics", "confidence": 0.5},
            ),
        ),
    ],
)
def test_ready_rejects_duplicate_contract_values(
    field: str, value: tuple[str] | tuple[dict[str, float | str], ...]
) -> None:
    payload = enrichment(EnrichmentStatus.READY).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize(
    ("status", "field", "value", "error"),
    [
        (
            EnrichmentStatus.PENDING,
            "failure_code",
            "timeout",
            "pending_invalid_metadata",
        ),
        (
            EnrichmentStatus.PROCESSING,
            "attempt_count",
            0,
            "processing_invalid_metadata",
        ),
        (
            EnrichmentStatus.PROCESSING,
            "enriched_at",
            UTC_VALUE,
            "processing_invalid_metadata",
        ),
        (EnrichmentStatus.FAILED, "attempt_count", 0, "failed_invalid_metadata"),
        (EnrichmentStatus.READY, "failure_code", "timeout", "ready_invalid_metadata"),
        (EnrichmentStatus.STALE, "enriched_at", None, "stale_invalid_metadata"),
        (EnrichmentStatus.STALE, "failure_code", "timeout", "stale_invalid_metadata"),
    ],
)
def test_lifecycle_rejects_each_invalid_metadata_combination(
    status: EnrichmentStatus, field: str, value: str | int | datetime | None, error: str
) -> None:
    payload = enrichment(status).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError, match=error):
        _ = ArticleEnrichment.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("content_type", "blog"), ("event_relevance", "viral")],
)
def test_controlled_classifications_reject_unknown_values(
    field: str, value: str
) -> None:
    payload = enrichment(EnrichmentStatus.READY).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError, match="literal_error"):
        _ = ArticleEnrichment.model_validate(payload)
