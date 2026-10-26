from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import HttpUrl, ValidationError

from backend.rag.domain.access import CallerScope
from backend.rag.domain.answer import (
    BriefingAnswer,
    Citation,
    Claim,
    ClaimDisplayBlock,
    Uncertainty,
    UncertaintyDisplayBlock,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding_policy import EmbeddingPolicy
from backend.rag.domain.temporal import TemporalClaim
from backend.shared.contracts import EvidenceSpan

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
SOURCE_ID = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
SPAN_ID = UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
CLAIM_ID = UUID("8c9e6679-7425-40de-944b-e07fc1f90ae7")
UTC_VALUE = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def content_hash(content: str) -> str:
    return f"sha256:{sha256(content.encode()).hexdigest()}"


def document(content: str = "Tiếng Việt 中文😀") -> ArticleDocument:
    normalized = "Tiếng Việt 中文😀"
    span = EvidenceSpan(
        span_id=SPAN_ID,
        article_id=ARTICLE_ID,
        content_hash=content_hash(normalized),
        start_offset=11,
        end_offset=14,
        excerpt="中文😀",
        span_fingerprint=content_hash("中文😀"),
        offset_basis="unicode_codepoint",
    )
    return ArticleDocument(
        schema_version="article_document.v1",
        article_id=ARTICLE_ID,
        source_id=SOURCE_ID,
        title="Bản tin",
        raw_content=content,
        normalization_version="nfc.v1",
        normalized_content=normalized,
        canonical_url=HttpUrl("https://example.com/article"),
        source_name="Example",
        source_domain="example.com",
        source_published_at=UTC_VALUE,
        ingested_at=UTC_VALUE,
        processed_at=UTC_VALUE,
        content_hash=content_hash(normalized),
        content_fingerprint=content_hash(normalized),
        visibility="internal",
        evidence_spans=(span,),
    )


def citation() -> Citation:
    return Citation(
        citation_id=UUID("9c9e6679-7425-40de-944b-e07fc1f90ae7"),
        claim_id=CLAIM_ID,
        article_id=ARTICLE_ID,
        evidence_span_id=SPAN_ID,
        source_title="Bản tin",
        source_reference=HttpUrl("https://example.com/article"),
    )


def test_document_normalizes_nfc_and_hashes_normalized_text() -> None:
    # Given / When
    result = document()

    # Then
    assert result.normalized_content == "Tiếng Việt 中文😀"
    assert result.content_hash == content_hash(result.normalized_content)
    assert result.content_fingerprint == content_hash(result.normalized_content)
    assert result.raw_content == "Tiếng Việt 中文😀"
    assert result.normalization_version == "nfc.v1"


def test_document_rejects_hash_or_fingerprint_not_matching_normalized_text() -> None:
    # Given
    payload = document().model_dump()

    # When / Then
    for field in ("content_hash", "content_fingerprint"):
        invalid = {**payload, field: content_hash("raw source")}
        with pytest.raises(
            ValidationError, match="normalized_content_identity_mismatch"
        ):
            _ = ArticleDocument.model_validate(invalid)


def test_span_offsets_are_unicode_code_points_on_nfc_content() -> None:
    # Given / When
    result = document()

    # Then
    span = result.evidence_spans[0]
    assert result.normalized_content[span.start_offset : span.end_offset] == "中文😀"
    assert span.end_offset - span.start_offset == 3
    assert span.span_fingerprint == content_hash("中文😀")


def test_document_rejects_span_fingerprint_not_matching_normalized_excerpt() -> None:
    # Given
    payload = document().model_dump()
    payload["evidence_spans"][0]["span_fingerprint"] = content_hash("raw source")

    # When / Then
    with pytest.raises(ValidationError, match="evidence_span_fingerprint_mismatch"):
        _ = ArticleDocument.model_validate(payload)


def test_evidence_span_rejects_fingerprint_not_matching_its_excerpt() -> None:
    # Given
    payload = document().evidence_spans[0].model_dump()
    payload["span_fingerprint"] = content_hash("FORGED EVIDENCE")

    # When / Then
    with pytest.raises(ValidationError, match="evidence_span_fingerprint_mismatch"):
        _ = EvidenceSpan.model_validate(payload)


def test_caller_scope_is_frozen_fixture_identity_with_policy_version() -> None:
    # Given / When
    scope = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )

    # Then
    assert scope.policy_id == "fixture-policy"
    with pytest.raises(ValidationError):
        scope.policy_version = "v2"
    with pytest.raises(ValidationError):
        _ = CallerScope.model_validate({**scope.model_dump(), "extra": True})
    with pytest.raises(ValidationError):
        _ = CallerScope.model_validate(
            {**scope.model_dump(), "principal_id": "another-principal"}
        )


def test_temporal_claim_requires_evidence_and_never_uses_publication_time() -> None:
    # Given
    temporal = TemporalClaim(
        value_state="known",
        start_at=UTC_VALUE,
        end_at=None,
        precision="minute",
        confidence=0.8,
        evidence_span_ids=(SPAN_ID,),
        relative_expression=None,
        reference_at=None,
        timezone_basis=None,
    )

    # When / Then
    assert temporal.start_at == UTC_VALUE
    with pytest.raises(ValidationError, match="empty_temporal_evidence"):
        _ = TemporalClaim.model_validate(
            {**temporal.model_dump(), "evidence_span_ids": ()}
        )
    with pytest.raises(ValidationError, match="unknown_temporal_value_has_timestamp"):
        _ = TemporalClaim.model_validate(
            {**temporal.model_dump(), "value_state": "unknown"}
        )
    with pytest.raises(ValidationError, match="relative_temporal_basis_required"):
        _ = TemporalClaim.model_validate(
            {
                **temporal.model_dump(),
                "relative_expression": "hôm qua",
            }
        )


def test_relative_temporal_claim_requires_utc_clock_and_timezone_basis() -> None:
    # Given / When
    temporal = TemporalClaim(
        value_state="known",
        start_at=UTC_VALUE,
        end_at=None,
        precision="day",
        confidence=0.8,
        evidence_span_ids=(SPAN_ID,),
        relative_expression="hôm qua",
        reference_at=UTC_VALUE,
        timezone_basis="UTC",
    )

    # Then
    assert temporal.relative_expression == "hôm qua"
    with pytest.raises(ValidationError):
        _ = TemporalClaim.model_validate(
            {**temporal.model_dump(), "reference_at": "2026-09-23T17:00:00+07:00"}
        )


def test_displayed_answer_is_deterministic_validated_blocks_only() -> None:
    # Given
    claim = Claim(
        claim_id=CLAIM_ID,
        text="Sự kiện đã xảy ra.",
        temporal_claim=None,
    )
    uncertainty = Uncertainty(
        uncertainty_id=UUID("ac9e6679-7425-40de-944b-e07fc1f90ae7"),
        category="temporal_uncertainty",
        explanation="Thời điểm chưa xác định.",
        claim_id=CLAIM_ID,
    )

    # When
    answer = BriefingAnswer(
        schema_version="briefing_answer.v1",
        answer="Sự kiện đã xảy ra.\n\nThời điểm chưa xác định.",
        claims=(claim,),
        citations=(citation(),),
        uncertainties=(uncertainty,),
        display_blocks=(
            ClaimDisplayBlock(kind="claim", claim_id=CLAIM_ID),
            UncertaintyDisplayBlock(
                kind="uncertainty", uncertainty_id=uncertainty.uncertainty_id
            ),
        ),
        source_documents=(document(),),
        retrieved_at=UTC_VALUE,
        model_version="model-1",
        index_version="index-1",
    )

    # Then
    assert answer.answer == "Sự kiện đã xảy ra.\n\nThời điểm chưa xác định."
    with pytest.raises(ValidationError, match="unknown_display_claim"):
        _ = BriefingAnswer.model_validate(
            {
                **answer.model_dump(exclude={"answer"}),
                "answer": "Sự kiện đã xảy ra.",
                "display_blocks": ({"kind": "claim", "claim_id": ARTICLE_ID},),
            }
        )
    with pytest.raises(ValidationError):
        _ = BriefingAnswer.model_validate(
            {**answer.model_dump(exclude={"answer"}), "answer": "Bịa đặt"}
        )


def test_embedding_policy_freezes_approved_1536_dimension() -> None:
    # Given / When
    policy = EmbeddingPolicy(
        model="text-embedding-3-small",
        dimension=1536,
        embedding_version="openai.text-embedding-3-small.v1",
    )

    # Then
    assert policy.dimension == 1536
    with pytest.raises(ValidationError):
        _ = EmbeddingPolicy.model_validate({**policy.model_dump(), "dimension": 3072})
