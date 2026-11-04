"""RetrieveEvidence with a search-index candidate source.

The index only proposes candidates; PostgreSQL (here the in-memory store)
re-checks current revision, tombstone, scope and metadata before evidence is
assembled, so a stale or tombstoned index entry can never be returned.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast, override

import pytest

from backend.rag.application.ports.reranker import (
    RerankerIdentity,
    RerankOutput,
    RerankRequest,
)
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.search_candidates import (
    IndexedCandidateSource,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.paragraph_chunking import PARAGRAPH_CHUNKER_VERSION
from backend.rag.domain.retrieval import (
    EmptyReason,
    QueryControl,
    RerankDegradeReason,
    RerankStatus,
    ResultStatus,
    RetrievalAccessDeniedError,
    RetrievalMode,
    RetrievalStorageError,
    VectorBranchStatus,
)
from backend.rag.domain.search_embedding import FAKE_BGE_M3, SearchEmbeddingIdentity
from backend.rag.domain.search_index import SearchFilters
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from briefing_support import query
from retrieval_support import CALLER
from search_support import START, SearchHarness, article, search_harness

if TYPE_CHECKING:
    from backend.rag.application.ports.access import AccessPolicy
    from backend.rag.application.ports.embeddings import BatchEmbeddingPort
    from backend.rag.application.ports.reranker import RerankerPort

NOW = datetime(2026, 4, 1, tzinfo=UTC)
PORT = (
    "Cảng Hải Phòng tạm đóng cửa bến Đình Vũ do bão số 3. "
    "Ban quản lý cảng cho biết 42 tàu phải neo chờ ngoài phao số 0.\n\n"
    "Theo thông báo, bến sẽ mở lại khi gió giảm dưới cấp 6. "
    "Các hãng tàu được đề nghị điều chỉnh lịch trình."
)
METRO = (
    "Tuyến metro số 1 Bến Thành - Suối Tiên đón 1,2 triệu lượt khách trong tháng đầu. "
    "Ban quản lý đường sắt đô thị TP. HCM công bố số liệu chiều qua.\n\n"
    "Giá vé dao động từ 7.000 đến 20.000 đồng mỗi lượt."
)


@dataclass(slots=True)
class World:
    """PostgreSQL truth (store) mirrored into the search world/index."""

    store: InMemoryRetrievalStore = field(default_factory=InMemoryRetrievalStore)
    search: SearchHarness = field(default_factory=search_harness)

    def add(self, document: ArticleDocument) -> ArticleDocument:
        self.store.add(document)
        self.search.world.persist(document)
        return document

    def build(self) -> None:
        _ = self.search.rebuild.execute(self.search.embedder.identity)
        _ = self.search.drain.execute()

    def retriever(self, **overrides: object) -> RetrieveEvidence:
        embedder = cast(
            "BatchEmbeddingPort | None",
            overrides.pop("embedder", self.search.embedder),
        )
        access = cast("AccessPolicy", overrides.pop("access", FixtureAccessPolicy()))
        fallback = cast("bool", overrides.pop("fallback_to_store", True))
        reranker = cast("RerankerPort", overrides.pop("reranker", IdentityReranker()))
        if overrides:
            detail = f"unknown test overrides: {sorted(overrides)}"
            raise AssertionError(detail)
        source = IndexedCandidateSource(
            index=self.search.index,
            embedder=embedder,
            generations=self.search.generations.active,
        )
        return RetrieveEvidence(
            store=self.store,
            embeddings=FakeEmbeddings(),
            embedding_identity=EmbeddingIdentity(),
            reranker=reranker,
            tokens=FixtureTokenCounter(),
            access=access,
            policy=bundled_retrieval_policy(),
            clock=lambda: NOW,
            candidates=source,
            fallback_to_store=fallback,
        )


def _world() -> tuple[World, ArticleDocument, ArticleDocument]:
    world = World()
    port = world.add(article(PORT, title="Cảng Hải Phòng đóng bến Đình Vũ"))
    metro = world.add(article(METRO, title="Metro số 1 đông khách"))
    world.build()
    return world, port, metro


def test_index_candidates_become_exact_evidence_with_provenance() -> None:
    world, port, _ = _world()

    result = world.retriever().execute(
        query("Bao nhiêu tàu neo chờ ở cảng Hải Phòng?"), CALLER, QueryControl(10)
    )

    assert result.status is ResultStatus.EVIDENCE
    assert result.articles[0].article_id == port.article_id
    assert result.retrieval_mode is RetrievalMode.HYBRID
    assert result.vector_branch_status is VectorBranchStatus.EXECUTED
    provenance = result.provenance
    assert provenance.search_backend == "memory"
    assert provenance.index_generation == f"g1:paragraph-900-1.v1:{FAKE_BGE_M3.key}"
    assert provenance.source_chunker_version == PARAGRAPH_CHUNKER_VERSION
    assert provenance.source_embedding_identity == FAKE_BGE_M3.key
    for span in result.articles[0].spans:
        start, end = span.span.start_offset, span.span.end_offset
        assert span.span.excerpt == port.normalized_content[start:end]


def test_stale_index_revision_is_dropped_by_the_postgres_final_check() -> None:
    world, port, _ = _world()
    corrected = article(
        PORT.replace("42 tàu", "38 tàu"), article_id=port.article_id, title=port.title
    )
    world.store.add(corrected)  # PostgreSQL moved on; outbox not drained yet

    result = world.retriever().execute(
        query("tàu neo chờ phao số 0 Đình Vũ"), CALLER, QueryControl(10)
    )

    assert all(a.content_hash != port.content_hash for a in result.articles)
    if result.status is ResultStatus.EMPTY:
        assert result.empty_reason is EmptyReason.DROPPED_AT_FINAL_CHECK
    assert result.metrics.dropped_at_final_check >= 1


def test_tombstoned_article_is_never_returned_even_before_drain() -> None:
    world, port, _ = _world()
    world.store.tombstone(port.article_id)

    result = world.retriever().execute(
        query("cảng Hải Phòng đóng bến Đình Vũ"), CALLER, QueryControl(10)
    )

    assert port.article_id not in {a.article_id for a in result.articles}


def test_index_outage_falls_back_to_postgres_when_allowed() -> None:
    world, port, _ = _world()
    world.search.index.unavailable = True

    result = world.retriever().execute(
        query("cảng Hải Phòng đóng bến Đình Vũ"), CALLER, QueryControl(10)
    )

    assert result.provenance.search_backend == "postgres_fallback"
    assert result.provenance.search_fallback_reason == "search_index_unavailable"
    assert result.articles[0].article_id == port.article_id


def test_index_outage_fails_closed_when_fallback_is_disabled() -> None:
    world, _, _ = _world()
    world.search.index.unavailable = True
    with pytest.raises(RetrievalStorageError):
        _ = world.retriever(fallback_to_store=False).execute(
            query("cảng Hải Phòng"), CALLER, QueryControl(10)
        )


def test_missing_active_generation_falls_back() -> None:
    world = World()
    _ = world.add(article(PORT, title="Cảng"))
    result = world.retriever().execute(
        query("cảng Hải Phòng đóng bến"), CALLER, QueryControl(10)
    )
    assert result.provenance.search_backend == "postgres_fallback"
    assert result.provenance.search_fallback_reason == "no_active_generation"


def test_denied_caller_never_reaches_the_index() -> None:
    world, _, _ = _world()
    searches = world.search.index.searches
    with pytest.raises(RetrievalAccessDeniedError):
        _ = world.retriever(access=FixtureAccessPolicy(revoked=True)).execute(
            query("cảng Hải Phòng"), CALLER, QueryControl(10)
        )
    assert world.search.index.searches == searches


def test_scope_isolation_in_both_branches() -> None:
    world, _, _ = _world()
    generation = world.search.generations.active()
    assert generation is not None
    other = SearchFilters(scope_id="another-tenant")
    vector = world.search.embedder.embed_query("cảng Hải Phòng")
    index = world.search.index
    assert index.lexical_search(generation, "cảng Hải Phòng", other, 50) == ()
    assert index.semantic_search(generation, vector, other, 50) == ()


def test_filter_parity_between_bm25_and_vector_branches() -> None:
    world = World()
    early = START - timedelta(days=30)
    docs = [
        article(PORT, title="Cảng A", domain="a.example", published=early),
        article(PORT + " Bản B.", title="Cảng B", domain="b.example", published=START),
        article(PORT + " Bản C.", title="Cảng C", domain="a.example", published=None),
        article(PORT + " Bản D.", title="Cảng D", domain="a.example", published=START),
    ]
    for doc in docs:
        _ = world.add(doc)
    world.build()
    generation = world.search.generations.active()
    assert generation is not None
    filters = SearchFilters(
        scope_id="fixture-internal",
        source_domains=("a.example",),
        published_after=START - timedelta(days=1),
    )
    index = world.search.index
    vector = world.search.embedder.embed_query("cảng Hải Phòng")
    lexical = {
        h.article_id for h in index.lexical_search(generation, "cảng", filters, 50)
    }
    semantic = {
        h.article_id for h in index.semantic_search(generation, vector, filters, 50)
    }
    assert lexical == semantic == {docs[3].article_id}


def test_embedding_identity_mismatch_disables_only_the_semantic_branch() -> None:
    world, port, _ = _world()
    other = FakeBgeM3Embeddings(
        identity=SearchEmbeddingIdentity("bge-m3-1024.v1", "BAAI/bge-m3", 1024)
    )
    result = world.retriever(embedder=other).execute(
        query("cảng Hải Phòng đóng bến Đình Vũ"), CALLER, QueryControl(10)
    )
    assert result.vector_branch_status is VectorBranchStatus.EMBEDDING_INCOMPATIBLE
    assert result.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert result.articles[0].article_id == port.article_id


class _BrokenEmbedder(FakeBgeM3Embeddings):
    @override
    def embed_query(self, text: str) -> tuple[float, ...]:
        del text
        msg = "model crashed"
        raise RuntimeError(msg)


def test_embedding_failure_keeps_lexical_results() -> None:
    world, _, _ = _world()
    result = world.retriever(embedder=_BrokenEmbedder()).execute(
        query("cảng Hải Phòng đóng bến Đình Vũ"), CALLER, QueryControl(10)
    )
    assert result.vector_branch_status is VectorBranchStatus.EMBEDDING_FAILED
    assert result.status is ResultStatus.EVIDENCE


@dataclass(frozen=True, slots=True)
class _SlowReranker:
    seconds: float

    @property
    def identity(self) -> RerankerIdentity:
        return RerankerIdentity("slow", "v1")

    def rerank(self, request: RerankRequest) -> RerankOutput:
        time.sleep(self.seconds)
        return RerankOutput(
            scores=tuple((c.candidate_id, 1.0) for c in request.candidates)
        )


def test_reranker_timeout_falls_back_to_fused_order_without_failing() -> None:
    world, port, _ = _world()
    slow = world.retriever(reranker=_SlowReranker(3.0))
    result = slow.execute(
        query("cảng Hải Phòng đóng bến Đình Vũ"), CALLER, QueryControl(10)
    )
    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.TIMEOUT
    assert result.articles[0].article_id == port.article_id


def test_rerank_can_be_switched_off_per_call() -> None:
    world, _, _ = _world()
    result = world.retriever().execute(
        query("cảng Hải Phòng đóng bến"),
        CALLER,
        QueryControl(10),
        RetrievalOptions(rerank_enabled=False),
    )
    assert result.rerank_status is RerankStatus.SKIPPED
