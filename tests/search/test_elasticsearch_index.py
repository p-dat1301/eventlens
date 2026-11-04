"""Elasticsearch adapter against a real cluster (``make search-index-test``).

Needs ``EVENTLENS_RAG_TEST_ELASTICSEARCH_URL`` (loopback); the make target
starts a disposable single-node container and fails on any skip. Each test
owns a unique index under the adapter prefix and deletes it afterwards.
"""

import os
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import cast, override
from uuid import uuid4

import pytest

from backend.rag.application.ports.search_index import RevisionWrite, SearchIndexError
from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.application.use_cases.search_projection import ProjectRevision
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.paragraph_chunking import PARAGRAPH_CHUNKER_VERSION
from backend.rag.domain.search_embedding import FAKE_BGE_M3
from backend.rag.domain.search_index import (
    GenerationStatus,
    HybridSearchConfig,
    IndexGeneration,
    SearchChunkDocument,
    SearchFilters,
)
from backend.rag.infra.elasticsearch.client import ElasticsearchClient, Json
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from search_support import START, article

PREFIX = "eventlens-rag-search-test"
SCOPE = SearchFilters(scope_id="fixture-internal")
PORT = (
    "Cảng Hải Phòng tạm đóng bến Đình Vũ do bão số 3.\n\n"
    "Chủ tịch UBND thành phố Nguyễn Văn Tùng yêu cầu các hãng tàu neo đậu an toàn."
)
METRO = (
    "Tuyến metro số 1 TP.HCM đón 1,2 triệu lượt khách.\n\n"
    "Ban quản lý đường sắt đô thị cho biết giá vé từ 7.000 đồng."
)


@pytest.fixture
def client() -> ElasticsearchClient:
    url = os.environ.get("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL")
    if url is None:
        pytest.skip("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL required")
    return ElasticsearchClient(url)


@pytest.fixture
def generation() -> IndexGeneration:
    return IndexGeneration(
        generation_id=1,
        index_name=f"{PREFIX}-{uuid4().hex[:12]}",
        chunk_version=PARAGRAPH_CHUNKER_VERSION,
        embedding_identity=FAKE_BGE_M3.key,
        embedding_model=FAKE_BGE_M3.model,
        embedding_dimensions=FAKE_BGE_M3.dimensions,
        status=GenerationStatus.BUILDING,
    )


@pytest.fixture
def index(
    client: ElasticsearchClient, generation: IndexGeneration
) -> Generator[ElasticsearchSearchIndex]:
    adapter = ElasticsearchSearchIndex(client, index_prefix=PREFIX)
    adapter.ensure_index(generation)
    yield adapter
    adapter.drop_index(generation)


def _project(
    generation: IndexGeneration, *documents: ArticleDocument
) -> tuple[SearchChunkDocument, ...]:
    projector = ProjectRevision(FakeBgeM3Embeddings(), None)
    return projector.execute_many(
        [
            ProjectionRevision(d.article_id, d, tombstoned=False, metadata=None)
            for d in documents
        ],
        generation,
    )


def _tokens(client: ElasticsearchClient, index_name: str, text: str) -> list[str]:
    _, body = client.request(
        "POST", f"/{index_name}/_analyze", {"analyzer": "vi_text", "text": text}
    )
    tokens = cast("list[dict[str, object]]", body["tokens"])
    return [str(token["token"]) for token in tokens]


def test_vietnamese_analyzer_keeps_accented_and_folded_tokens(
    index: ElasticsearchSearchIndex,
    client: ElasticsearchClient,
    generation: IndexGeneration,
) -> None:
    del index
    tokens = _tokens(client, generation.index_name, "Hà Nội, Nguyễn Văn Tùng, Đình Vũ")
    for token in ("hà", "ha", "nội", "noi", "nguyễn", "nguyen", "đình", "dinh"):
        assert token in tokens
    # Recorded limitation: syllables, not words; "TP.HCM" stays one token.
    assert _tokens(client, generation.index_name, "TP.HCM") == ["tp.hcm"]


def test_bm25_matches_unaccented_queries_and_proper_names(
    index: ElasticsearchSearchIndex, generation: IndexGeneration
) -> None:
    port, metro = article(PORT, title="Cảng Hải Phòng"), article(METRO, title="Metro")
    result = index.bulk_index(generation, _project(generation, port, metro))
    assert result.failed == 0
    for text in ("cang hai phong", "Nguyễn Văn Tùng", "nguyen van tung"):
        hits = index.lexical_search(generation, text, SCOPE, 5)
        assert hits[0].article_id == port.article_id, text


def test_upsert_revision_replaces_old_chunks_and_delete_removes_all(
    index: ElasticsearchSearchIndex, generation: IndexGeneration
) -> None:
    original = article(PORT)
    _ = index.bulk_index(generation, _project(generation, original))
    corrected = article(PORT + " Đính chính.", article_id=original.article_id)

    result = index.upsert_revision(
        generation, original.article_id, _project(generation, corrected), version=1
    )

    assert result.deleted >= 1
    digests = index.revision_digests(generation)
    assert [(d.article_id, d.content_hash) for d in digests] == [
        (original.article_id, corrected.content_hash)
    ]
    assert index.delete_article(generation, original.article_id, version=2) >= 1
    assert index.status(generation).documents == 0


def test_filters_apply_identically_to_bm25_and_knn(
    index: ElasticsearchSearchIndex, generation: IndexGeneration
) -> None:
    docs = [
        article(PORT, domain="a.example", published=START - timedelta(days=40)),
        article(PORT + " B.", domain="b.example", published=START),
        article(PORT + " C.", domain="a.example", published=None),
        article(PORT + " D.", domain="a.example", published=START),
    ]
    _ = index.bulk_index(generation, _project(generation, *docs))
    filters = SearchFilters(
        scope_id="fixture-internal",
        source_domains=("a.example",),
        published_after=START - timedelta(days=1),
    )
    vector = FakeBgeM3Embeddings().embed_query("cảng Hải Phòng")
    lexical = {
        h.article_id for h in index.lexical_search(generation, "cảng", filters, 50)
    }
    semantic = {
        h.article_id for h in index.semantic_search(generation, vector, filters, 50)
    }
    assert lexical == semantic == {docs[3].article_id}
    other_scope = SearchFilters(scope_id="another-tenant")
    assert index.lexical_search(generation, "cảng", other_scope, 50) == ()
    assert index.semantic_search(generation, vector, other_scope, 50) == ()


def test_hybrid_rrf_reports_branch_ranks(
    index: ElasticsearchSearchIndex, generation: IndexGeneration
) -> None:
    port, metro = article(PORT), article(METRO)
    _ = index.bulk_index(generation, _project(generation, port, metro))
    vector = FakeBgeM3Embeddings().embed_query("bão số 3 cảng")
    fused = index.hybrid_search(
        generation, "bão số 3 cảng", vector, SCOPE, HybridSearchConfig()
    )
    assert fused[0].hit.article_id == port.article_id
    assert fused[0].lexical_rank == 1
    assert fused[0].semantic_rank is not None
    assert fused[0].fused_rank == 1


def test_health_status_and_owned_prefix(
    index: ElasticsearchSearchIndex,
    generation: IndexGeneration,
    client: ElasticsearchClient,
) -> None:
    health = index.health(generation)
    assert health.reachable
    assert health.index_exists
    assert health.engine_version is not None
    foreign = IndexGeneration(
        2,
        "someone-elses-index",
        PARAGRAPH_CHUNKER_VERSION,
        None,
        None,
        None,
        GenerationStatus.ACTIVE,
    )
    with pytest.raises(SearchIndexError):
        index.drop_index(foreign)
    down = ElasticsearchSearchIndex(
        ElasticsearchClient("http://127.0.0.1:9", timeout_seconds=0.5), PREFIX
    )
    assert not down.health(generation).reachable
    del client


def test_fence_orders_writes_and_tombstones_on_a_real_cluster(
    index: ElasticsearchSearchIndex, generation: IndexGeneration
) -> None:
    old = article(PORT)
    new = article(PORT + " Đính chính.", article_id=old.article_id)
    newer = index.write_revisions(
        generation, (RevisionWrite(old.article_id, 10, _project(generation, new)),)
    )
    stale = index.write_revisions(
        generation, (RevisionWrite(old.article_id, 4, _project(generation, old)),)
    )
    assert (newer.applied, stale.superseded, stale.indexed) == (1, 1, 0)
    assert [d.content_hash for d in index.revision_digests(generation)] == [
        new.content_hash
    ]
    replay = index.write_revisions(
        generation, (RevisionWrite(old.article_id, 10, _project(generation, new)),)
    )
    assert replay.applied == 1  # same version is idempotent
    assert index.delete_article(generation, old.article_id, version=11) >= 1
    late = index.write_revisions(
        generation, (RevisionWrite(old.article_id, 10, _project(generation, new)),)
    )
    assert late.superseded == 1
    assert index.revision_digests(generation) == ()
    assert index.status(generation).documents == 0  # fences are not counted


@dataclass(frozen=True, slots=True)
class _DelayedChunkClient(ElasticsearchClient):
    """Finish a newer write after the old fence, before its chunk request."""

    before_chunk: Callable[[], None] = lambda: None
    armed: list[bool] = field(default_factory=lambda: [True], repr=False)

    @override
    def bulk(self, path: str, lines: Sequence[Mapping[str, object]]) -> Json:
        if self.armed and lines and lines[1].get("doc_kind") == "chunk":
            self.armed.clear()
            self.before_chunk()
        return ElasticsearchClient.bulk(self, path, lines)


@pytest.mark.parametrize("change", ["metadata", "content_return", "tombstone"])
def test_delayed_chunk_write_preserves_the_newer_completed_projection(
    index: ElasticsearchSearchIndex,
    client: ElasticsearchClient,
    generation: IndexGeneration,
    change: str,
) -> None:
    original = article(PORT)
    documents = _project(generation, original)
    version = 3 if change == "content_return" else 2
    current = tuple(
        replace(d, topics=("economy",), projection_version=version) for d in documents
    )

    def finish_newer_write() -> None:
        if change == "content_return":
            correction = article(PORT + " Đính chính.", article_id=original.article_id)
            middle = index.write_revisions(
                generation,
                (
                    RevisionWrite(
                        original.article_id, 2, _project(generation, correction)
                    ),
                ),
            )
            assert middle.applied == 1
        if change == "tombstone":
            _ = index.delete_article(generation, original.article_id, version=version)
        else:
            latest = index.write_revisions(
                generation, (RevisionWrite(original.article_id, version, current),)
            )
            assert latest.applied == 1
            assert index.status(generation).documents == len(current)

    delayed = ElasticsearchSearchIndex(
        _DelayedChunkClient(client.base_url, before_chunk=finish_newer_write),
        index_prefix=PREFIX,
    )
    old = delayed.write_revisions(
        generation, (RevisionWrite(original.article_id, 1, documents),)
    )

    assert (old.applied, old.superseded, old.failed) == (0, 1, 0)
    if change == "tombstone":
        assert index.status(generation).documents == 0
        return
    assert index.status(generation).documents == len(current)
    for document in current:
        _, response = client.request(
            "GET", f"/{generation.index_name}/_doc/{document.chunk_id}"
        )
        source = cast("Json", response["_source"])
        assert source["projection_version"] == version
        assert source["topics"] == ["economy"]
    replay = index.write_revisions(
        generation, (RevisionWrite(original.article_id, version, current),)
    )
    assert replay.applied == 1
