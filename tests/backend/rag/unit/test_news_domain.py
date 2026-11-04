"""News backend domain rules: catalog filters/cursors, summaries, scopes."""

import importlib.util
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from backend.rag.application.ports.summary import SummaryModelRequest
from backend.rag.application.use_cases.catalog import resolve_sort
from backend.rag.domain.catalog import (
    CatalogRequestError,
    CatalogSort,
    CursorCodec,
    CursorPosition,
    catalog_filters,
)
from backend.rag.domain.conversation import (
    ArticlePin,
    QuestionInvalidError,
    ScopeInvalidError,
    ScopeType,
    conversation_scope,
    retrieval_query,
    scope_from_canonical,
    validate_question,
)
from backend.rag.domain.summary import (
    SummaryFailureCode,
    SummaryProviderError,
    SummarySource,
    parse_draft,
    span_fingerprint,
    validate_draft,
)
from backend.rag.domain.taxonomy import derive_labels, location_alias_key
from backend.rag.infra.llm.summarizers import FixtureSummarizer
from backend.shared.contracts import ArticleEnrichment

ROOT = Path(__file__).parents[4]
MIGRATION = (
    ROOT / "src/backend/rag/infra/postgres/revisions/versions/rag_0004_news_backend.py"
)
CODEC = CursorCodec(b"k" * 32)
CONTENT = (
    "Cầu Phú Mỹ tạm đóng một làn từ 22 giờ. "
    "Sở Giao thông TP.HCM khuyến cáo người dân đi đường vòng."
)


def _migration_alias_key() -> Callable[[str], str]:
    spec = importlib.util.spec_from_file_location("rag_0004", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("Callable[[str], str]", getattr(module, "alias_key"))  # noqa: B009


# --- catalog filters and cursors ------------------------------------------


def test_filters_require_timezone_and_normalize_to_utc() -> None:
    with pytest.raises(CatalogRequestError) as caught:
        _ = catalog_filters(published_after=datetime(2026, 10, 29))  # noqa: DTZ001
    assert caught.value.code == "timezone_required"

    hanoi = timezone(timedelta(hours=7))
    filters = catalog_filters(published_after=datetime(2026, 10, 29, tzinfo=hanoi))
    assert filters.published_after == datetime(2026, 10, 28, 17, tzinfo=UTC)


def test_filters_reject_reversed_range_and_too_many_ids() -> None:
    after = datetime(2026, 10, 29, tzinfo=UTC)
    with pytest.raises(CatalogRequestError) as reversed_range:
        _ = catalog_filters(
            published_after=after, published_before=after - timedelta(1)
        )
    assert reversed_range.value.code == "invalid_publication_range"
    with pytest.raises(CatalogRequestError) as too_many:
        _ = catalog_filters(topic_ids=tuple(f"t{i}" for i in range(21)))
    assert too_many.value.code == "too_many_topic_ids"
    with pytest.raises(CatalogRequestError) as too_long:
        _ = catalog_filters(q="x" * 201)
    assert too_long.value.code == "query_too_long"


def test_equal_bounds_are_an_empty_half_open_range() -> None:
    moment = datetime(2026, 10, 29, tzinfo=UTC)
    assert catalog_filters(published_after=moment, published_before=moment).empty_range


def test_filter_digest_ignores_id_order_and_duplicates() -> None:
    one = catalog_filters(topic_ids=("health", "economy", "health"))
    two = catalog_filters(topic_ids=("economy", "health"))
    assert one.digest() == two.digest()


def test_cursor_round_trips_and_is_bound_to_filters_and_sort() -> None:
    filters = catalog_filters(topic_ids=("economy",))
    position = CursorPosition(datetime(2026, 1, 1, tzinfo=UTC), uuid4(), None)
    token = CODEC.encode(filters, CatalogSort.LATEST, position)

    assert CODEC.decode(token, filters, CatalogSort.LATEST) == position
    for other_filters, sort in (
        (catalog_filters(topic_ids=("health",)), CatalogSort.LATEST),
        (filters, CatalogSort.OLDEST),
    ):
        with pytest.raises(CatalogRequestError) as caught:
            _ = CODEC.decode(token, other_filters, sort)
        assert caught.value.code == "cursor_mismatch"


@pytest.mark.parametrize("token", ["", "abc", "x" * 2000])
def test_garbage_or_foreign_cursor_is_invalid(token: str) -> None:
    filters = catalog_filters()
    foreign = CursorCodec(b"z" * 32).encode(
        filters, CatalogSort.LATEST, CursorPosition(None, uuid4(), None)
    )
    for value in (token, foreign):
        with pytest.raises(CatalogRequestError) as caught:
            _ = CODEC.decode(value, filters, CatalogSort.LATEST)
        assert caught.value.code == "invalid_cursor"


def test_relevance_needs_a_query_and_is_the_query_default() -> None:
    assert resolve_sort(None, catalog_filters(q="cầu")) is CatalogSort.RELEVANCE
    assert resolve_sort(None, catalog_filters()) is CatalogSort.LATEST
    with pytest.raises(CatalogRequestError) as caught:
        _ = resolve_sort("relevance", catalog_filters())
    assert caught.value.code == "relevance_requires_query"


# --- taxonomy ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["TP. Hồ Chí Minh", "Đắk Lắk", "Bà Rịa - Vũng Tàu", "Thừa Thiên Huế"]
)
def test_runtime_alias_key_matches_the_frozen_migration_function(text: str) -> None:
    frozen = _migration_alias_key()
    assert location_alias_key(text) == frozen(text)


def test_location_labels_need_one_unambiguous_alias_match() -> None:
    article_id = uuid4()
    metadata = _metadata(
        article_id,
        topics=["transportation"],
        locations=[
            ("city", "TP.HCM", ["Thành phố Hồ Chí Minh"]),
            ("facility", "Cầu Phú Mỹ", []),
            ("city", "Atlantis", []),
        ],
    )
    derived = derive_labels(
        metadata,
        {"tp hcm": "vn-ho-chi-minh", "thanh pho ho chi minh": "vn-ho-chi-minh"},
    )
    assert {(lab.kind, lab.label_id) for lab in derived.labels} == {
        ("topic", "transportation"),
        ("location", "vn-ho-chi-minh"),
    }
    assert derived.unmatched_locations == 1  # Atlantis; facilities are skipped


def _metadata(
    article_id: UUID,
    *,
    topics: list[str],
    locations: list[tuple[str, str, list[str]]],
) -> ArticleEnrichment:
    content_hash = "sha256:" + "a" * 64
    span_id = uuid4()
    return ArticleEnrichment.model_validate_json(
        json.dumps(
            {
                "schema_version": "article_enrichment.v1",
                "article_id": str(article_id),
                "content_hash": content_hash,
                "enrichment_version": "v1",
                "model_version": "m",
                "prompt_version": "p",
                "status": "READY",
                "topics": topics,
                "content_type": "news_report",
                "event_relevance": "actionable_event",
                "entities": [],
                "locations": [
                    {
                        "location_id": None,
                        "location_type": kind,
                        "canonical_name": name,
                        "surface_forms": forms,
                        "evidence_span_ids": [str(span_id)],
                        "confidence": 0.9,
                    }
                    for kind, name, forms in locations
                ],
                "suggested_tags": [],
                "evidence_spans": [
                    {
                        "span_id": str(span_id),
                        "article_id": str(article_id),
                        "content_hash": content_hash,
                        "start_offset": 0,
                        "end_offset": 3,
                        "excerpt": "abc",
                        "span_fingerprint": span_fingerprint("abc"),
                        "offset_basis": "unicode_codepoint",
                    }
                ],
                "field_confidence": [],
                "enriched_at": "2026-10-29T00:00:00Z",
                "attempt_count": 1,
                "failure_code": None,
            }
        )
    )


# --- summaries ---------------------------------------------------------------


def _draft(quote: str) -> str:
    return json.dumps(
        {
            "summary": "Tóm tắt.",
            "key_points": [{"text": "Ý", "evidence": [{"quote": quote}]}],
        }
    )


def test_quotes_become_exact_offsets_and_fingerprints() -> None:
    quote = "Sở Giao thông TP.HCM khuyến cáo"
    points = validate_draft(parse_draft(_draft(quote)), CONTENT)
    span = points[0].spans[0]
    assert CONTENT[span.start_offset : span.end_offset] == quote == span.excerpt
    assert span.span_fingerprint == span_fingerprint(quote)


def test_quote_not_in_content_rejects_the_whole_draft() -> None:
    with pytest.raises(SummaryProviderError) as caught:
        _ = validate_draft(
            parse_draft(_draft("Sở Giao thông Hà Nội khuyến cáo")), CONTENT
        )
    assert caught.value.code is SummaryFailureCode.INVALID_EVIDENCE


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"summary": "x", "key_points": []}),
        json.dumps({"summary": "x", "key_points": [{"text": "y", "evidence": []}]}),
        json.dumps({"summary": "x", "key_points": [], "extra": 1}),
    ],
)
def test_malformed_drafts_are_rejected(content: str) -> None:
    with pytest.raises(SummaryProviderError) as caught:
        _ = parse_draft(content)
    assert caught.value.code is SummaryFailureCode.MALFORMED_OUTPUT


def test_fixture_summarizer_output_is_valid_and_quotes_the_revision() -> None:
    source = SummarySource(uuid4(), "sha256:" + "b" * 64, "Tiêu đề", CONTENT, "N", None)
    response = FixtureSummarizer().summarize(SummaryModelRequest(source, 5.0, 100))
    points = validate_draft(parse_draft(response.content), CONTENT)
    assert points
    assert all(
        CONTENT[s.start_offset : s.end_offset] == s.excerpt
        for p in points
        for s in p.spans
    )


def test_fixture_summarizer_keeps_long_sentences_within_the_quote_limit() -> None:
    content = "Một câu rất dài " * 80 + "kết thúc."
    source = SummarySource(uuid4(), "sha256:" + "d" * 64, "T", content, "N", None)
    response = FixtureSummarizer().summarize(SummaryModelRequest(source, 5.0, 100))
    assert validate_draft(parse_draft(response.content), content)


def test_only_transient_provider_failures_retry() -> None:
    retryable = {c for c in SummaryFailureCode if c.retryable}
    assert retryable == {
        SummaryFailureCode.PROVIDER_TIMEOUT,
        SummaryFailureCode.PROVIDER_UNAVAILABLE,
    }


# --- conversation scopes ------------------------------------------------------


def test_scope_shapes_are_validated() -> None:
    with pytest.raises(ScopeInvalidError):
        _ = conversation_scope(ScopeType.FILTERED, None, ())
    with pytest.raises(ScopeInvalidError):
        _ = conversation_scope(ScopeType.ARTICLES, None, ())
    same = uuid4()
    with pytest.raises(ScopeInvalidError):
        _ = conversation_scope(
            ScopeType.ARTICLES, None, (ArticlePin(same, None), ArticlePin(same, None))
        )
    with pytest.raises(ScopeInvalidError):
        _ = conversation_scope(ScopeType.ALL, catalog_filters(), ())


def test_stored_scope_round_trips() -> None:
    filtered = conversation_scope(
        ScopeType.FILTERED,
        catalog_filters(
            topic_ids=("transportation",),
            location_ids=("vn-ho-chi-minh",),
            published_after=datetime(2026, 10, 28, 17, tzinfo=UTC),
        ),
        (),
    )
    pinned = conversation_scope(
        ScopeType.ARTICLES, None, (ArticlePin(uuid4(), "sha256:" + "c" * 64),)
    )
    for scope in (filtered, pinned, conversation_scope(ScopeType.ALL, None, ())):
        assert scope_from_canonical(scope.canonical()) == scope
        assert scope_from_canonical(scope.canonical()).digest() == scope.digest()


def test_follow_up_uses_previous_question_never_answers() -> None:
    previous = ("Cầu Phú Mỹ đóng làn lúc mấy giờ?",)
    assert retrieval_query("Còn hướng ngược lại?", previous) == (
        "Cầu Phú Mỹ đóng làn lúc mấy giờ?\nCòn hướng ngược lại?"
    )
    long_question = " ".join(["từ"] * 20)
    assert retrieval_query(long_question, previous) == long_question
    assert retrieval_query("Câu đầu tiên?", ()) == "Câu đầu tiên?"


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_questions_are_bounded(question: str) -> None:
    with pytest.raises(QuestionInvalidError):
        _ = validate_question(question)
