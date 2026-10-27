"""Elasticsearch BM25 adapter on a real node (EVENTLENS_RAG_TEST_ELASTICSEARCH_URL).

Skips without the variable; ``scripts/rag/search-index-test.sh`` (CI job
``search-index``) sets it and fails on any skip.
"""

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.article_document import ArticleDocument, normalized_content_hash
from backend.rag.domain.chunking import DEFAULT_POLICY
from backend.rag.evaluation.search_bench.bench import build_chunks
from backend.rag.infra.search.elasticsearch_bm25 import (
    ANALYZERS,
    ElasticsearchBm25Index,
    SearchEngineError,
    analyzer_digest,
)
from backend.rag.infra.search.memory_bm25 import MemoryBm25Index

EVERYTHING = RetrievalScope((), None, None, None)


@pytest.fixture
def es_url() -> str:
    value = os.environ.get("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL")
    if value is None:
        pytest.skip("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL required for Elasticsearch")
    return value


def document(
    n: int, text: str, domain: str, published: datetime | None
) -> ArticleDocument:
    digest = normalized_content_hash(text)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": UUID(f"00000000-0000-4000-8000-{n:012d}"),
            "source_id": UUID("00000000-0000-4000-8000-000000000099"),
            "title": f"Bài {n}",
            "raw_content": text,
            "normalization_version": "nfc.v1",
            "normalized_content": text,
            "canonical_url": f"https://{domain}/{n}",
            "source_name": "Nguồn",
            "source_domain": domain,
            "source_published_at": published,
            "ingested_at": datetime(2026, 10, 26, tzinfo=UTC),
            "processed_at": datetime(2026, 10, 26, tzinfo=UTC),
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


DOCS = (
    document(
        1,
        "Giá vàng thế giới tăng mạnh khi đồng USD suy yếu.",
        "a.vn",
        datetime(2026, 10, 15, tzinfo=UTC),
    ),
    document(
        2,
        "Giá xăng giảm nhẹ trong kỳ điều hành chiều nay.",
        "b.vn",
        datetime(2026, 10, 16, tzinfo=UTC),
    ),
    document(3, "Đội tuyển Việt Nam thắng trận đầu tiên tại vòng loại.", "a.vn", None),
    document(
        4,
        "Vàng miếng SJC và vàng nhẫn cùng tăng giá sáng nay. " * 30,
        "b.vn",
        datetime(2026, 10, 17, tzinfo=UTC),
    ),
)


@pytest.fixture
def index(es_url: str) -> ElasticsearchBm25Index:
    built = ElasticsearchBm25Index(es_url, "vi-standard.v1", DEFAULT_POLICY.version)
    built.rebuild(build_chunks(DOCS, DEFAULT_POLICY))
    return built


def test_identity_records_engine_version_and_analyzer_digest(
    index: ElasticsearchBm25Index,
) -> None:
    identity = index.identity
    assert identity.engine == "elasticsearch"
    assert identity.engine_version[0].isdigit()
    assert identity.analyzer_digest == analyzer_digest("vi-standard.v1")
    assert identity.similarity == "BM25(k1=1.2,b=0.75)"
    assert index.size_bytes() > 0


def test_bm25_ranking_matches_reference_top_hits(index: ElasticsearchBm25Index) -> None:
    reference = MemoryBm25Index()
    reference.rebuild(build_chunks(DOCS, DEFAULT_POLICY))

    for query in ("giá vàng", "đội tuyển Việt Nam", "xăng"):
        engine = index.search(query, EVERYTHING, 10)
        expected = reference.search(query, EVERYTHING, 10)
        assert {h.chunk.chunk_id for h in engine} == {
            h.chunk.chunk_id for h in expected
        }
        assert engine[0].chunk.article_id == expected[0].chunk.article_id


def test_scope_filters_apply_before_depth(index: ElasticsearchBm25Index) -> None:
    domain = index.search("giá vàng", RetrievalScope(("a.vn",), None, None, None), 10)
    window = index.search(
        "vàng",
        RetrievalScope(
            (),
            datetime(2026, 10, 16, tzinfo=UTC),
            datetime(2026, 10, 18, tzinfo=UTC),
            None,
        ),
        10,
    )
    null_time = index.search(
        "đội tuyển",
        RetrievalScope((), datetime(2000, 1, 1, tzinfo=UTC), None, None),
        10,
    )
    allowed = index.search(
        "giá", RetrievalScope((), None, None, (DOCS[1].article_id,)), 10
    )

    assert {h.chunk.article_id for h in domain} == {DOCS[0].article_id}
    assert {h.chunk.article_id for h in window} == {DOCS[3].article_id}
    assert null_time == ()
    assert {h.chunk.article_id for h in allowed} == {DOCS[1].article_id}


def test_hits_carry_exact_chunk_offsets(index: ElasticsearchBm25Index) -> None:
    hits = index.search("SJC", EVERYTHING, 50)
    text = DOCS[3].normalized_content

    assert len(hits) > 1
    for item in hits:
        chunk = item.chunk
        assert chunk.content_hash == DOCS[3].content_hash
        assert 0 <= chunk.start_offset < chunk.end_offset <= len(text)
        assert chunk.chunker_version == DEFAULT_POLICY.version


def test_rebuild_replaces_content_and_is_repeatable(
    index: ElasticsearchBm25Index,
) -> None:
    index.rebuild(build_chunks(DOCS[:1], DEFAULT_POLICY))
    assert {h.chunk.article_id for h in index.search("giá", EVERYTHING, 10)} == {
        DOCS[0].article_id
    }
    index.rebuild(build_chunks(DOCS[:1], DEFAULT_POLICY))
    assert len(index.search("giá", EVERYTHING, 10)) == 1


@pytest.mark.parametrize("analyzer", sorted(ANALYZERS))
def test_every_versioned_analyzer_builds(es_url: str, analyzer: str) -> None:
    built = ElasticsearchBm25Index(es_url, analyzer, DEFAULT_POLICY.version)
    built.rebuild(build_chunks(DOCS, DEFAULT_POLICY))
    assert built.search("vàng", EVERYTHING, 5)


def test_unreachable_engine_is_a_typed_error() -> None:
    dead = ElasticsearchBm25Index("http://127.0.0.1:9", "vi-standard.v1", "x", 0.5)
    with pytest.raises(SearchEngineError):
        _ = dead.search("x", EVERYTHING, 1)
