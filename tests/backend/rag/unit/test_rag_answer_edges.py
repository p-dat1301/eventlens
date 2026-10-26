from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.rag.domain.answer import BriefingAnswer
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
SPAN_ID = UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")
CLAIM_ID = UUID("8c9e6679-7425-40de-944b-e07fc1f90ae7")
UTC_VALUE = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
SPAN_HASH = (
    "sha256:" + "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
)


def answer_fixture() -> BriefingAnswer:
    document = {
        "schema_version": "article_document.v1",
        "article_id": ARTICLE_ID,
        "source_id": UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
        "title": "Title",
        "raw_content": "hello",
        "normalization_version": "nfc.v1",
        "normalized_content": "hello",
        "canonical_url": "https://example.com/article",
        "source_name": "Example",
        "source_domain": "example.com",
        "source_published_at": UTC_VALUE,
        "ingested_at": UTC_VALUE,
        "processed_at": UTC_VALUE,
        "content_hash": SPAN_HASH,
        "content_fingerprint": SPAN_HASH,
        "visibility": "internal",
        "evidence_spans": (
            {
                "span_id": SPAN_ID,
                "article_id": ARTICLE_ID,
                "content_hash": SPAN_HASH,
                "start_offset": 0,
                "end_offset": 5,
                "excerpt": "hello",
                "span_fingerprint": SPAN_HASH,
                "offset_basis": "unicode_codepoint",
            },
        ),
    }
    return BriefingAnswer.model_validate(
        {
            "schema_version": "briefing_answer.v1",
            "answer": "A fact",
            "claims": (
                {"claim_id": CLAIM_ID, "text": "A fact", "temporal_claim": None},
            ),
            "citations": (
                {
                    "citation_id": UUID("9c9e6679-7425-40de-944b-e07fc1f90ae7"),
                    "claim_id": CLAIM_ID,
                    "article_id": ARTICLE_ID,
                    "evidence_span_id": SPAN_ID,
                    "source_title": "Title",
                    "source_reference": "https://example.com/article",
                },
            ),
            "uncertainties": (),
            "display_blocks": ({"kind": "claim", "claim_id": CLAIM_ID},),
            "source_documents": (document,),
            "retrieved_at": UTC_VALUE,
            "model_version": "model-1",
            "index_version": "index-1",
        }
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("claim_id", ARTICLE_ID, "unknown_citation_claim"),
        ("article_id", SPAN_ID, "unknown_citation_article"),
        ("evidence_span_id", ARTICLE_ID, "unknown_evidence_span"),
    ],
)
def test_citation_rejects_each_unknown_link(
    field: str, value: UUID, error: str
) -> None:
    answer = answer_fixture()
    payload = answer.model_dump()
    citation = answer.citations[0].model_dump()
    payload["citations"] = ({**citation, field: value},)
    with pytest.raises(ValidationError, match=error):
        _ = BriefingAnswer.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "briefing_answer.v2"),
        ("model_version", " "),
        ("index_version", " "),
    ],
)
def test_answer_rejects_invalid_version_identity(field: str, value: str) -> None:
    payload = answer_fixture().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError, match=field):
        _ = BriefingAnswer.model_validate(payload)


def test_answer_rejects_non_utc_retrieval_time() -> None:
    payload = answer_fixture().model_dump()
    payload["retrieved_at"] = datetime(
        2026, 9, 23, 17, 0, tzinfo=timezone(timedelta(hours=7))
    )
    with pytest.raises(ValidationError, match="retrieved_at"):
        _ = BriefingAnswer.model_validate(payload)


def test_answer_and_query_forbid_extra_fields_and_mutation() -> None:
    answer = answer_fixture()
    query = BriefingQuery(
        schema_version="briefing_query.v1",
        query="news",
        publication_time_basis="source_published_at",
        result_limit=10,
        context_budget=100,
    )
    for model in (answer, query):
        with pytest.raises(ValidationError, match="frozen_instance"):
            model.__setattr__("schema_version", "changed")
        with pytest.raises(ValidationError, match="extra_forbidden"):
            _ = type(model).model_validate({**model.model_dump(), "unknown": True})


def test_temporal_evidence_must_be_cited_by_its_claim() -> None:
    answer = answer_fixture()
    payload = answer.model_dump()
    payload["claims"] = (
        {
            **answer.claims[0].model_dump(),
            "temporal_claim": {
                "value_state": "known",
                "start_at": UTC_VALUE,
                "end_at": None,
                "precision": "day",
                "confidence": 0.5,
                "evidence_span_ids": (ARTICLE_ID,),
                "relative_expression": None,
                "reference_at": None,
                "timezone_basis": None,
            },
        },
    )
    with pytest.raises(ValidationError, match="unknown_temporal_evidence"):
        _ = BriefingAnswer.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("normalization_version", "nfc.v2", "normalization_version"),
        ("schema_version", "article_document.v2", "schema_version"),
        (
            "processed_at",
            datetime(2026, 9, 23, 17, 0, tzinfo=timezone(timedelta(hours=7))),
            "processed_at",
        ),
    ],
)
def test_document_rejects_invalid_version_or_non_utc_time(
    field: str, value: str | datetime, error: str
) -> None:
    document = answer_fixture().source_documents[0]
    with pytest.raises(ValidationError, match=error):
        _ = ArticleDocument.model_validate({**document.model_dump(), field: value})


def test_document_forbids_unknown_fields() -> None:
    document = answer_fixture().source_documents[0]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _ = ArticleDocument.model_validate({**document.model_dump(), "unknown": True})
