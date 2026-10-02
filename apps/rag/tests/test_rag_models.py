from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens_contracts import EvidenceSpan
from eventlens_rag.domain.answer import (
    BriefingAnswer,
    Citation,
    Claim,
    ClaimDisplayBlock,
    Uncertainty,
    UncertaintyDisplayBlock,
)
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.query import BriefingQuery

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
SOURCE_ID = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
SPAN_ID = UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
CLAIM_ID = UUID("8c9e6679-7425-40de-944b-e07fc1f90ae7")
HASH = "sha256:" + "a" * 64
UTC_VALUE = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def document() -> ArticleDocument:
    content = "hello world"
    content_hash = f"sha256:{sha256(content.encode()).hexdigest()}"
    span = EvidenceSpan(
        span_id=SPAN_ID,
        article_id=ARTICLE_ID,
        content_hash=content_hash,
        start_offset=0,
        end_offset=5,
        excerpt="hello",
        span_fingerprint=f"sha256:{sha256(b'hello').hexdigest()}",
        offset_basis="unicode_codepoint",
    )
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": ARTICLE_ID,
            "source_id": SOURCE_ID,
            "title": "Title",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://example.com/article",
            "source_name": "Example",
            "source_domain": "example.com",
            "source_published_at": UTC_VALUE,
            "ingested_at": UTC_VALUE,
            "processed_at": UTC_VALUE,
            "content_hash": content_hash,
            "content_fingerprint": content_hash,
            "visibility": "internal",
            "evidence_spans": (span,),
        }
    )


def test_article_document_json_round_trip_is_frozen() -> None:
    parsed = ArticleDocument.model_validate_json(document().model_dump_json())
    assert parsed == document()
    with pytest.raises(ValidationError):
        parsed.title = "changed"


@pytest.mark.parametrize(
    ("start", "end", "excerpt"),
    [(-1, 2, "he"), (4, 2, ""), (0, 5, "other"), (0, 20, "hello")],
)
def test_document_rejects_invalid_evidence_offsets(
    start: int, end: int, excerpt: str
) -> None:
    payload = document().model_dump()
    payload["evidence_spans"] = [
        {
            **payload["evidence_spans"][0],
            "start_offset": start,
            "end_offset": end,
            "excerpt": excerpt,
        }
    ]
    with pytest.raises(ValidationError):
        _ = ArticleDocument.model_validate(payload)


def test_answer_requires_claim_citation_and_valid_span() -> None:
    claim = Claim(claim_id=CLAIM_ID, text="A fact", temporal_claim=None)
    citation = Citation.model_validate(
        {
            "citation_id": UUID("9c9e6679-7425-40de-944b-e07fc1f90ae7"),
            "claim_id": CLAIM_ID,
            "article_id": ARTICLE_ID,
            "evidence_span_id": SPAN_ID,
            "source_title": "Title",
            "source_reference": "https://example.com/article",
        }
    )
    answer = BriefingAnswer(
        schema_version="briefing_answer.v1",
        answer="A fact",
        claims=(claim,),
        citations=(citation,),
        uncertainties=(),
        display_blocks=(ClaimDisplayBlock(kind="claim", claim_id=CLAIM_ID),),
        source_documents=(document(),),
        retrieved_at=UTC_VALUE,
        model_version="model-1",
        index_version="index-1",
    )
    assert BriefingAnswer.model_validate_json(answer.model_dump_json()) == answer
    with pytest.raises(ValidationError, match="claim_missing_citation"):
        _ = BriefingAnswer.model_validate({**answer.model_dump(), "citations": ()})
    for field, value in (
        ("source_title", "Forged title"),
        ("source_reference", "https://forged.example/article"),
    ):
        payload = answer.model_dump()
        payload["citations"][0][field] = value
        with pytest.raises(ValidationError):
            _ = BriefingAnswer.model_validate(payload)
    with pytest.raises(ValidationError, match="duplicate_source_document"):
        _ = BriefingAnswer.model_validate(
            {**answer.model_dump(), "source_documents": (document(), document())}
        )


def test_answer_allows_explicit_abstention_only() -> None:
    answer = BriefingAnswer(
        schema_version="briefing_answer.v1",
        answer="No supporting source was retrieved.",
        claims=(),
        citations=(),
        uncertainties=(
            Uncertainty(
                uncertainty_id=UUID("ac9e6679-7425-40de-944b-e07fc1f90ae7"),
                category="insufficient_evidence",
                explanation="No supporting source was retrieved.",
            ),
        ),
        display_blocks=(
            UncertaintyDisplayBlock(
                kind="uncertainty",
                uncertainty_id=UUID("ac9e6679-7425-40de-944b-e07fc1f90ae7"),
            ),
        ),
        source_documents=(),
        retrieved_at=UTC_VALUE,
        model_version="model-1",
        index_version="index-1",
    )
    assert not answer.claims


def test_query_rejects_non_utc_and_reversed_range() -> None:
    with pytest.raises(ValidationError):
        _ = BriefingQuery(
            schema_version="briefing_query.v1",
            query="news",
            publication_time_basis="source_published_at",
            result_limit=10,
            context_budget=100,
            published_after=UTC_VALUE.replace(hour=11),
            published_before=UTC_VALUE,
        )
    with pytest.raises(ValidationError):
        _ = BriefingQuery.model_validate(
            {
                "schema_version": "briefing_query.v1",
                "query": "news",
                "topic_filters": ("unsupported",),
                "publication_time_basis": "source_published_at",
                "result_limit": 10,
                "context_budget": 100,
            }
        )
