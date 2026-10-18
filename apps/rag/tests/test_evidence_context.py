"""Evidence assembly: article/token caps, span integrity and Unicode offsets."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import TypeAdapter
from retrieval_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CALLER,
    POLICY,
    MemoryStore,
    article,
    briefing,
    control,
    service,
)
from retrieval_support import (  # pyright: ignore[reportImplicitRelativeImport]
    FIXTURES as FIXTURE_ROOT,
)

from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.adapters.retrieval_smoke import questions
from eventlens_rag.application.evidence_context import (
    EvidenceAssembler,
    PreparedArticle,
    PreparedSpan,
)
from eventlens_rag.application.retrieve import RetrievalOptions
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.retrieval import (
    Branch,
    EmptyReason,
    ResultStatus,
    RetrievalMode,
    RetrievedSpan,
    build_span,
)

COUNTER = FixtureTokenCounter()
LINE = TypeAdapter(dict[str, object])
MULTILINGUAL = (
    "Cảng Hải Phòng đóng cửa lúc 8 giờ. 港口今天关闭\uff0c渡轮停运。"
    "Café closed 🚢🚢 after the storm; crews inspect cranes."
)


def prepared(
    document: ArticleDocument, ranges: list[tuple[int, int]]
) -> PreparedArticle:
    spans: list[PreparedSpan] = []
    for index, (start, end) in enumerate(ranges):
        span = build_span(
            document.article_id,
            document.content_hash,
            document.normalized_content,
            start,
            end,
        )
        spans.append(
            PreparedSpan(
                span=span,
                template=RetrievedSpan(
                    span=span,
                    chunk_id=UUID(int=index + 1),
                    chunk_index=index,
                    branches=(Branch.LEXICAL,),
                    lexical_rank=index + 1,
                    vector_rank=None,
                    fused_score=1 / 61,
                    rerank_score=None,
                    truncated=False,
                ),
            )
        )
    return PreparedArticle(
        article_id=document.article_id,
        content_hash=document.content_hash,
        normalized_content=document.normalized_content,
        title=document.title,
        source_name=document.source_name,
        source_domain=document.source_domain,
        source_published_at=document.source_published_at,
        retrieval_basis=RetrievalMode.LEXICAL_ONLY,
        vector_generation=None,
        metadata=None,
        duplicate_article_ids=(),
        spans=tuple(spans),
    )


def two_articles() -> tuple[PreparedArticle, ...]:
    first = article("Harbor gate closes for crane inspection. " * 6)
    second = article(MULTILINGUAL, published=datetime(2026, 3, 2, tzinfo=UTC))
    return (
        prepared(first, [(0, 120), (120, len(first.normalized_content))]),
        prepared(second, [(0, len(second.normalized_content))]),
    )


def full_count(articles: tuple[PreparedArticle, ...]) -> int:
    assembler = EvidenceAssembler(COUNTER, POLICY.output)
    return assembler.assemble(articles, 12_000).context.token_count


def assert_span_integrity(document_text: str, spans: list[RetrievedSpan]) -> None:
    for item in spans:
        span = item.span
        assert document_text[span.start_offset : span.end_offset] == span.excerpt
        digest = sha256(span.excerpt.encode()).hexdigest()
        assert span.span_fingerprint == f"sha256:{digest}"


def test_exact_limit_keeps_everything_untruncated() -> None:
    articles = two_articles()
    budget = full_count(articles)

    result = EvidenceAssembler(COUNTER, POLICY.output).assemble(articles, budget)

    assert result.context.token_count == budget
    assert [len(a.spans) for a in result.articles] == [2, 1]
    assert result.context.truncated_spans == 0
    assert not any(s.truncated for a in result.articles for s in a.spans)


def test_one_over_truncates_last_span_at_token_boundary_with_new_identity() -> None:
    articles = two_articles()
    budget = full_count(articles) - 1

    result = EvidenceAssembler(COUNTER, POLICY.output).assemble(articles, budget)

    assert result.context.token_count <= budget
    assert result.context.truncated_spans == 1
    last = result.articles[-1].spans[-1]
    original = articles[-1].spans[-1].span
    assert last.truncated
    assert last.span.span_id != original.span_id
    assert last.span.start_offset == original.start_offset
    assert last.span.end_offset < original.end_offset
    assert original.excerpt.startswith(last.span.excerpt)
    assert_span_integrity(
        articles[-1].normalized_content, list(result.articles[-1].spans)
    )


def test_counts_whole_serialized_context_not_code_points() -> None:
    articles = two_articles()

    result = EvidenceAssembler(COUNTER, POLICY.output).assemble(articles, 12_000)

    text = result.context.text
    assert result.context.token_count == COUNTER.count(text)
    assert result.context.token_count != len(text)
    lines = [LINE.validate_json(line) for line in text.split("\n")]
    assert [line["rank"] for line in lines] == [1, 2]
    assert set(lines[0]) == {
        "rank",
        "article_id",
        "content_hash",
        "title",
        "source_name",
        "source_domain",
        "source_published_at",
        "spans",
    }
    assert lines[0]["source_published_at"] is None
    assert result.context.tokenizer_id == "fixture-regex-tokens"


def test_small_budget_stops_at_first_span_that_does_not_fit() -> None:
    articles = two_articles()
    first_line_only = EvidenceAssembler(COUNTER, POLICY.output).assemble(
        articles[:1], 12_000
    )
    budget = first_line_only.context.token_count + 5  # too small for article 2

    result = EvidenceAssembler(COUNTER, POLICY.output).assemble(articles, budget)

    assert [a.article_id for a in result.articles] == [articles[0].article_id]
    assert result.context.omitted_articles == 1
    assert result.context.token_count <= budget


def test_budget_without_room_for_one_minimal_span_returns_nothing() -> None:
    articles = two_articles()

    result = EvidenceAssembler(COUNTER, POLICY.output).assemble(articles, 40)

    assert result.articles == ()
    assert result.context.text == ""
    assert result.context.token_count == 0


def test_multilingual_truncation_keeps_code_point_offsets_valid() -> None:
    document = article(MULTILINGUAL * 3)
    item = prepared(document, [(0, len(document.normalized_content))])
    header_only = EvidenceAssembler(COUNTER, POLICY.output)
    full = header_only.assemble((item,), 12_000).context.token_count

    truncated = 0
    for budget in range(full - 60, full):
        result = header_only.assemble((item,), budget)
        if not result.articles:
            continue
        spans = list(result.articles[0].spans)
        truncated += sum(span.truncated for span in spans)
        assert_span_integrity(document.normalized_content, spans)
        assert result.context.token_count <= budget
    assert truncated > 0


def corpus(count: int) -> MemoryStore:
    store = MemoryStore()
    start = datetime(2026, 3, 1, tzinfo=UTC)
    for index in range(count):
        store.add(
            article(
                f"Harbor bulletin number {index}: crane inspection {index} done.",
                published=start + timedelta(hours=index),
            )
        )
    return store


@pytest.mark.parametrize(
    ("available", "limit", "expected"),
    [(21, 20, 20), (21, 100, 20), (20, 20, 20), (8, 5, 5)],
)
def test_article_cap_is_min_of_result_limit_and_twenty(
    available: int, limit: int, expected: int
) -> None:
    store = corpus(available)

    result = service(store, embeddings=None).execute(
        briefing("harbor crane inspection", result_limit=limit),
        CALLER,
        control(),
        RetrievalOptions(vector_enabled=False),
    )

    assert result.status is ResultStatus.EVIDENCE
    assert len(result.articles) == expected
    assert [a.rank for a in result.articles] == list(range(1, expected + 1))
    assert store.calls.count("current_articles") == 1  # batched final check


def test_query_budget_lower_than_default_is_respected_and_never_raised() -> None:
    store = corpus(10)
    retrieve = service(store, embeddings=None)
    options = RetrievalOptions(vector_enabled=False)

    small = retrieve.execute(
        briefing("harbor crane", context_budget=120), CALLER, control(), options
    )
    lowered = retrieve.execute(
        briefing("harbor crane", context_budget=500),
        CALLER,
        control(),
        RetrievalOptions(vector_enabled=False, remaining_context_budget=120),
    )
    not_raised = retrieve.execute(
        briefing("harbor crane", context_budget=120),
        CALLER,
        control(),
        RetrievalOptions(vector_enabled=False, remaining_context_budget=9_000),
    )

    assert small.context.token_budget == 120
    assert small.context.token_count <= 120
    assert lowered.context.token_budget == 120
    assert not_raised.context.token_budget == 120
    assert len(small.articles) < 10


def test_budget_too_small_is_a_distinct_empty_reason() -> None:
    result = service(corpus(3), embeddings=None).execute(
        briefing("harbor crane", context_budget=10),
        CALLER,
        control(),
        RetrievalOptions(vector_enabled=False),
    )

    assert result.status is ResultStatus.EMPTY
    assert result.empty_reason is EmptyReason.CONTEXT_BUDGET_TOO_SMALL


def test_zero_hits_is_empty_no_matches_with_metrics() -> None:
    result = service(corpus(3), embeddings=None).execute(
        briefing("zzzqqq"), CALLER, control(), RetrievalOptions(vector_enabled=False)
    )

    assert result.status is ResultStatus.EMPTY
    assert result.empty_reason is EmptyReason.NO_MATCHES
    assert result.articles == ()
    assert result.context.text == ""
    assert result.metrics.storage_calls == 1
    assert result.retrieval_mode is RetrievalMode.NONE


def test_metrics_and_context_never_carry_storage_uri_or_raw_rows() -> None:
    result = service(corpus(2), embeddings=None).execute(
        briefing("harbor crane"),
        CALLER,
        control(),
        RetrievalOptions(vector_enabled=False),
    )

    rendered = repr(result.metrics)
    assert "Harbor bulletin" not in rendered
    assert "s3://" not in result.context.text
    assert "postgresql://" not in rendered
    assert "canonical_url" not in result.context.text


def test_smoke_question_set_has_ten_questions_with_denominators() -> None:
    items = questions(FIXTURE_ROOT)

    assert len(items) == 10
    assert sum(len(q.expected) for q in items) == 12
    assert sum(1 for q in items if not q.expected) == 2  # abstain cases
