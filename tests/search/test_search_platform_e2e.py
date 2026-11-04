"""End to end on real PostgreSQL + Elasticsearch (``make search-index-test``).

persist (ingest path) -> outbox -> drain -> hybrid search -> briefing with
citation -> source viewer; then correction, tombstone, rebuild, reconcile,
Elasticsearch outage with PostgreSQL fallback, retry and dead letter.
Needs ``EVENTLENS_RAG_TEST_ELASTICSEARCH_URL`` and ``EVENTLENS_RAG_DATABASE_URL``
(the make target provides owned, throwaway instances; skips fail that run).
"""

import os
import time
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.rag.application.use_cases.inspect_source import SourceStaleError
from backend.rag.application.use_cases.search_candidates import IndexedCandidateSource
from backend.rag.composition import (
    FIXTURE_CALLER,
    SearchRuntime,
    SearchSettings,
    build_runtime,
    build_search_runtime,
    compose_briefing,
)
from backend.rag.domain.article_scope import ArticleRestriction
from backend.rag.domain.outbox import OutboxRetryPolicy, OutboxStatus
from backend.rag.domain.retrieval import RetrievalStorageError
from backend.rag.infra.elasticsearch.client import ElasticsearchClient
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.postgres.engine import dispose_engines, rag_engine
from backend.rag.infra.postgres.search_platform import PostgresSearchGenerations
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.shared.contracts.bundle import ArticleBundle
from briefing_support import query
from schema_support import prepare_schema
from search_support import article

PORT = (
    "Cảng Lạch Huyện Xanh tạm dừng tiếp nhận tàu vì bão số 2.\n\n"
    "Theo giám đốc Trần Minh Khoa, 42 tàu container đang neo chờ ngoài phao số 0."
)
PORT_FIXED = PORT.replace("42 tàu", "38 tàu")
METRO = (
    "Tuyến metro số 4 Hà Đông - Mê Linh chạy thử với 12 đoàn tàu.\n\n"
    "Ban quản lý cho biết tuyến dài 23,6 km và có 18 nhà ga."
)
QUESTION = "Bao nhiêu tàu container neo chờ ngoài phao số 0?"
BUNDLE = Path(__file__).parents[2] / "fixtures/article_bundle.v1.json"


@pytest.fixture
def es_url() -> str:
    value = os.environ.get("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL")
    if value is None:
        pytest.skip("EVENTLENS_RAG_TEST_ELASTICSEARCH_URL required")
    return value


@pytest.fixture
def db_url() -> Generator[str]:
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required")
    schema = "search_e2e_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    scoped = make_conninfo(base, options=f"-c search_path={schema},public")
    prepare_schema(scoped)
    yield scoped
    dispose_engines()
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


@pytest.fixture
def runtime(db_url: str, es_url: str) -> Generator[SearchRuntime]:
    settings = SearchSettings.from_environment(
        {
            "EVENTLENS_RAG_SEARCH_BACKEND": "elasticsearch",
            "EVENTLENS_RAG_ELASTICSEARCH_URL": es_url,
            "EVENTLENS_RAG_SEARCH_EMBEDDINGS": "fake-bge-m3",
        }
    )
    built = build_search_runtime(db_url, settings)
    built.rebuild.index_prefix = f"eventlens-rag-search-e2e-{uuid4().hex[:8]}"
    yield built
    generations = PostgresSearchGenerations(rag_engine(db_url))
    for generation_id in range(1, 20):
        generation = generations.get(generation_id)
        if generation is not None:
            built.index.drop_index(generation)


def _briefing(db_url: str, index: ElasticsearchSearchIndex, *, fallback: bool):  # noqa: ANN202
    engine = rag_engine(db_url)
    return compose_briefing(
        PostgresRetrievalStore(engine),
        references=PostgresSourceReferences(engine),
        candidates=IndexedCandidateSource(
            index=index,
            embedder=FakeBgeM3Embeddings(),
            generations=PostgresSearchGenerations(engine).active,
        ),
        fallback_to_store=fallback,
    )


def test_postgres_outbox_elasticsearch_briefing_lifecycle(  # noqa: PLR0915
    db_url: str, es_url: str, runtime: SearchRuntime
) -> None:
    store = PostgresRagStore(rag_engine(db_url))
    port = article(PORT, title="Cảng Lạch Huyện Xanh dừng tiếp nhận tàu")
    metro = article(METRO, title="Metro số 4 chạy thử")
    store.persist_article(port)
    store.persist_article(metro)
    # The rag-worker ingest path (ArticleBundle -> document + PENDING job).
    bundle = ArticleBundle.model_validate_json(BUNDLE.read_text(encoding="utf-8"))
    receipt = build_runtime(db_url).ingest.execute(bundle.article, FIXTURE_CALLER)
    assert runtime.outbox.counts()[OutboxStatus.PENDING] == 3

    built = runtime.rebuild.execute(FakeBgeM3Embeddings().identity)
    drained = runtime.drain.execute()
    assert built.revisions == 3
    ingested = {
        (d.article_id, d.content_hash)
        for d in ElasticsearchSearchIndex(ElasticsearchClient(es_url)).revision_digests(
            built.generation
        )
    }
    assert (receipt.article_id, receipt.content_hash) in ingested
    assert drained.failed == 0
    assert runtime.outbox.counts()[OutboxStatus.PENDING] == 0
    es = ElasticsearchSearchIndex(ElasticsearchClient(es_url))

    # Search + citation + source viewer on the current revision.
    briefing = _briefing(db_url, es, fallback=True)
    outcome = briefing.use_case.execute(query(QUESTION))
    assert outcome.retrieval.provenance.search_backend == "elasticsearch"
    assert outcome.retrieval.articles[0].article_id == port.article_id
    cited = outcome.cited_sources[0]
    view = briefing.sources.execute(cited.source_ref, FIXTURE_CALLER)
    start, end = view.span.start_offset, view.span.end_offset
    assert view.span.excerpt == port.normalized_content[start:end]

    # Correction: the index only holds the new revision; old citation is stale.
    fixed = article(PORT_FIXED, article_id=port.article_id, title=port.title)
    store.persist_article(fixed)
    assert runtime.drain.execute().done == 1
    digests = {
        (d.article_id, d.content_hash) for d in es.revision_digests(built.generation)
    }
    assert (port.article_id, fixed.content_hash) in digests
    assert (port.article_id, port.content_hash) not in digests
    with pytest.raises(SourceStaleError):
        _ = briefing.sources.execute(cited.source_ref, FIXTURE_CALLER)
    again = briefing.use_case.execute(query(QUESTION))
    assert again.retrieval.articles[0].content_hash == fixed.content_hash
    fresh_view = briefing.sources.execute(
        again.cited_sources[0].source_ref, FIXTURE_CALLER
    )
    assert "38 tàu" in fresh_view.normalized_content

    # Tombstone removes every chunk of the article.
    store.tombstone_article(metro.article_id)
    assert runtime.drain.execute().done == 1
    assert metro.article_id not in {
        d.article_id for d in es.revision_digests(built.generation)
    }

    # Reconcile is clean; a rebuild yields the same revisions in a new index.
    assert runtime.reconcile.execute(deep=True).consistent
    rebuilt = runtime.rebuild.execute(FakeBgeM3Embeddings().identity)
    assert rebuilt.generation.index_name != built.generation.index_name
    assert rebuilt.embedded == 0  # vectors from the PostgreSQL cache
    assert {
        (d.article_id, d.content_hash) for d in es.revision_digests(rebuilt.generation)
    } == {
        (port.article_id, fixed.content_hash),
        (receipt.article_id, receipt.content_hash),
    }

    # Elasticsearch outage: fallback to PostgreSQL, or fail closed when disabled.
    dead = ElasticsearchSearchIndex(
        ElasticsearchClient("http://127.0.0.1:9", timeout_seconds=0.5)
    )
    served = _briefing(db_url, dead, fallback=True).use_case.execute(query(QUESTION))
    assert served.retrieval.provenance.search_backend == "postgres_fallback"
    assert served.retrieval.articles[0].content_hash == fixed.content_hash
    with pytest.raises(RetrievalStorageError):
        _ = _briefing(db_url, dead, fallback=False).use_case.execute(query(QUESTION))

    # Outage during drain: retry with backoff, then dead letter, then requeue.
    settings = SearchSettings.from_environment(
        {
            "EVENTLENS_RAG_SEARCH_BACKEND": "elasticsearch",
            "EVENTLENS_RAG_ELASTICSEARCH_URL": es_url,
            "EVENTLENS_RAG_SEARCH_EMBEDDINGS": "fake-bge-m3",
        }
    )
    broken = build_search_runtime(db_url, settings, index=dead)
    broken.drain.policy = OutboxRetryPolicy(max_attempts=2, base_seconds=0.2)
    store.persist_article(article(METRO + " Bổ sung.", title="Metro bổ sung"))
    first = broken.drain.execute()
    time.sleep(0.5)
    second = broken.drain.execute()
    assert (first.retried, second.failed) == (1, 1)
    assert runtime.outbox.counts()[OutboxStatus.FAILED] == 1
    assert runtime.outbox.requeue_failed() == 1
    assert runtime.drain.execute().done == 1
    assert runtime.reconcile.execute(deep=True).consistent


def test_conversation_article_scope_is_an_elasticsearch_filter(
    db_url: str, runtime: SearchRuntime
) -> None:
    store = PostgresRagStore(rag_engine(db_url))
    port = article(PORT, title="Cảng Lạch Huyện Xanh dừng tiếp nhận tàu")
    metro = article(METRO, title="Metro số 4 chạy thử")
    store.persist_article(port)
    store.persist_article(metro)
    _ = runtime.rebuild.execute(FakeBgeM3Embeddings().identity)
    _ = runtime.drain.execute()
    index = runtime.index
    assert isinstance(index, ElasticsearchSearchIndex)
    briefing = _briefing(db_url, index, fallback=False)

    unscoped = briefing.use_case.execute(query(QUESTION))
    scoped = briefing.use_case.execute(
        query(QUESTION),
        scope=ArticleRestriction({metro.article_id: metro.content_hash}),
    )

    assert port.article_id in {a.article_id for a in unscoped.retrieval.articles}
    assert {a.article_id for a in scoped.retrieval.articles} <= {metro.article_id}
    assert {c.citation.article_id for c in scoped.cited_sources} <= {metro.article_id}
    assert scoped.retrieval.provenance.search_backend == "elasticsearch"
