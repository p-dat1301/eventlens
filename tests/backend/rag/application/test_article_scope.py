"""Server-resolved article scope before retrieval (store and index branches).

The restriction is intersected into ``allowed_article_ids`` before either
branch runs (so the index filter and the PostgreSQL SQL both see it), and the
final current-revision check drops anything outside it or at another
revision. Nothing out of scope reaches the model payload or a citation.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from backend.rag.application.ports.synthesis import SynthesisRequest
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.search_candidates import (
    IndexedCandidateSource,
)
from backend.rag.domain.article_scope import ArticleRestriction
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.retrieval import EmptyReason, QueryControl, ResultStatus
from backend.rag.domain.retrieval_tuning import FieldBoosts
from backend.rag.domain.search_index import IndexGeneration, SearchFilters, SearchHit
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from backend.rag.infra.search.memory_index import InMemorySearchIndex
from briefing_support import QUESTION, harness, query
from retrieval_support import CALLER
from search_support import article, search_harness

if TYPE_CHECKING:
    from backend.rag.application.ports.search_index import SearchIndexPort

NOW = datetime(2026, 4, 1, tzinfo=UTC)
PORT = "Cảng Hải Phòng tạm đóng cửa bến Đình Vũ do bão số 3 theo thông báo."
METRO = (
    "Tuyến metro số 1 Bến Thành - Suối Tiên đón khách, cảng Hải Phòng không liên quan."
)


@dataclass(slots=True)
class SpyIndex:
    """Records the filters every index call receives."""

    inner: InMemorySearchIndex
    seen: list[SearchFilters] = field(default_factory=list[SearchFilters])

    def __getattr__(self, name: str) -> object:
        return cast("object", getattr(self.inner, name))

    def lexical_search(
        self,
        generation: IndexGeneration,
        text: str,
        filters: SearchFilters,
        size: int,
        boosts: FieldBoosts | None = None,
    ) -> tuple[SearchHit, ...]:
        self.seen.append(filters)
        return self.inner.lexical_search(generation, text, filters, size, boosts)

    def semantic_search(
        self,
        generation: IndexGeneration,
        vector: tuple[float, ...],
        filters: SearchFilters,
        size: int,
    ) -> tuple[SearchHit, ...]:
        self.seen.append(filters)
        return self.inner.semantic_search(generation, vector, filters, size)


def _retriever(
    store: InMemoryRetrievalStore, candidates: object = None
) -> RetrieveEvidence:
    return RetrieveEvidence(
        store=store,
        embeddings=FakeEmbeddings(),
        embedding_identity=EmbeddingIdentity(),
        reranker=IdentityReranker(),
        tokens=FixtureTokenCounter(),
        access=FixtureAccessPolicy(),
        policy=bundled_retrieval_policy(),
        clock=lambda: NOW,
        candidates=cast("IndexedCandidateSource | None", candidates),
    )


def _docs() -> tuple[InMemoryRetrievalStore, UUID, str, UUID, str]:
    store = InMemoryRetrievalStore()
    port = article(PORT, title="Cảng Hải Phòng đóng bến")
    metro = article(METRO, title="Metro số 1")
    store.add(port)
    store.add(metro)
    return (
        store,
        port.article_id,
        port.content_hash,
        metro.article_id,
        metro.content_hash,
    )


def test_store_branches_never_return_articles_outside_the_scope() -> None:
    store, port, _, metro, metro_hash = _docs()
    unscoped = _retriever(store).execute(
        query("cảng Hải Phòng"), CALLER, QueryControl(5.0)
    )
    assert port in {a.article_id for a in unscoped.articles}

    scoped = _retriever(store).execute(
        query("cảng Hải Phòng"),
        CALLER,
        QueryControl(5.0),
        RetrievalOptions(article_scope=ArticleRestriction({metro: metro_hash})),
    )
    assert {a.article_id for a in scoped.articles} <= {metro}


def test_a_scope_revision_that_changed_is_dropped_by_the_final_check() -> None:
    store, port, _, _, _ = _docs()
    stale = ArticleRestriction({port: "sha256:" + "0" * 64})
    result = _retriever(store).execute(
        query("cảng Hải Phòng"),
        CALLER,
        QueryControl(5.0),
        RetrievalOptions(article_scope=stale),
    )
    assert result.articles == ()


def test_empty_scope_is_an_empty_result_not_an_unscoped_search() -> None:
    store, _, _, _, _ = _docs()
    result = _retriever(store).execute(
        query("cảng Hải Phòng"),
        CALLER,
        QueryControl(5.0),
        RetrievalOptions(article_scope=ArticleRestriction({})),
    )
    assert result.status is ResultStatus.EMPTY
    assert result.empty_reason is EmptyReason.NO_MATCHES


def test_index_branch_receives_the_scope_as_a_filter() -> None:
    search = search_harness()
    store = InMemoryRetrievalStore()
    port = article(PORT, title="Cảng Hải Phòng đóng bến")
    metro = article(METRO, title="Metro số 1")
    for document in (port, metro):
        store.add(document)
        search.world.persist(document)
    _ = search.rebuild.execute(search.embedder.identity)
    _ = search.drain.execute()
    spy = SpyIndex(cast("InMemorySearchIndex", cast("object", search.index)))
    source = IndexedCandidateSource(
        index=cast("SearchIndexPort", cast("object", spy)),
        embedder=search.embedder,
        generations=search.generations.active,
    )
    result = _retriever(store, source).execute(
        query("cảng Hải Phòng"),
        CALLER,
        QueryControl(5.0),
        RetrievalOptions(
            article_scope=ArticleRestriction({metro.article_id: metro.content_hash})
        ),
    )
    assert spy.seen
    assert all(f.allowed_article_ids == (metro.article_id,) for f in spy.seen)
    assert {a.article_id for a in result.articles} <= {metro.article_id}


def _sent(requests: list[SynthesisRequest]) -> set[str]:
    found: set[str] = set()
    for request in requests:
        for message in request.serialized.messages:
            if message.role == "user":
                body = cast(
                    "dict[str, list[dict[str, str]]]", json.loads(message.content)
                )
                found.update(s["article_id"] for s in body.get("sources", []))
    return found


def test_model_payload_and_citations_stay_inside_the_scope() -> None:
    h = harness()
    documents = list(h.store.documents.values())
    unscoped = h.runtime.use_case.execute(query(QUESTION))
    cited = {c.citation.article_id for c in unscoped.cited_sources}
    assert cited
    keep = next(d for d in documents if d.article_id not in cited)
    h.model.requests.clear()

    outcome = h.runtime.use_case.execute(
        query(QUESTION), scope=ArticleRestriction({keep.article_id: keep.content_hash})
    )

    allowed = {str(keep.article_id)}
    assert _sent(h.model.requests) <= allowed
    assert {str(c.citation.article_id) for c in outcome.cited_sources} <= allowed
    assert {str(a.article_id) for a in outcome.retrieval.articles} <= allowed
