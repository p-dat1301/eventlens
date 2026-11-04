"""Handoff batch end to end on real PostgreSQL + Elasticsearch.

validate -> ImportHandoff (revision + provenance + outbox in one transaction)
-> rebuild + drain -> Elasticsearch -> briefing query -> source viewer on the
exact NFC slice; then a repeat import queues nothing new. Needs
``EVENTLENS_RAG_TEST_ELASTICSEARCH_URL`` and ``EVENTLENS_RAG_DATABASE_URL``
(``make search-index-test``; skips fail that run).
"""

import os
from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.rag.application.handoff_validation import validate_handoff
from backend.rag.application.use_cases.import_handoff import ImportHandoff
from backend.rag.application.use_cases.search_candidates import IndexedCandidateSource
from backend.rag.composition import (
    FIXTURE_CALLER,
    SearchRuntime,
    SearchSettings,
    build_search_runtime,
    compose_briefing,
)
from backend.rag.domain.outbox import OutboxStatus
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.elasticsearch.client import ElasticsearchClient
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.postgres.engine import dispose_engines, rag_engine
from backend.rag.infra.postgres.search_platform import PostgresSearchGenerations
from backend.rag.infra.postgres.source_versions import PostgresSourceVersions
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from briefing_support import query
from handoff_support import batch, record
from schema_support import prepare_schema

PORT = (
    "Cảng Lạch Huyện Xanh tạm dừng tiếp nhận tàu vì bão số 2.\n\n"
    "Theo giám đốc Trần Minh Khoa, 42 tàu container\u200b đang neo chờ ngoài phao số 0."
)
METRO = (
    "Tuyến metro số 4 Hà Đông - Mê Linh chạy thử với 12 đoàn tàu.\n\n"
    "Ban quản lý cho biết tuyến dài 23,6 km và có 18 nhà ga."
)
QUESTION = "Bao nhiêu tàu container neo chờ ngoài phao số 0?"


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
    schema = "handoff_e2e_" + uuid4().hex
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
    built.rebuild.index_prefix = f"eventlens-rag-search-handoff-e2e-{uuid4().hex[:8]}"
    yield built
    generations = PostgresSearchGenerations(rag_engine(db_url))
    for generation_id in range(1, 10):
        generation = generations.get(generation_id)
        if generation is not None:
            built.index.drop_index(generation)


def test_handoff_import_outbox_index_query_and_source_viewer(
    db_url: str, es_url: str, runtime: SearchRuntime
) -> None:
    records = [
        record(content=PORT, word_count=len(PORT.split()), title="Cảng dừng tàu"),
        record(content=METRO, word_count=len(METRO.split()), title="Metro số 4"),
    ]
    report = validate_handoff(*batch(records))
    engine = rag_engine(db_url)
    importer = ImportHandoff(
        PostgresSourceVersions(engine),
        FixtureAccessPolicy(),
        lambda: datetime(2026, 10, 28, tzinfo=UTC),
    )

    receipt = importer.execute(report, FIXTURE_CALLER)

    assert receipt.revisions_inserted == 2
    assert runtime.outbox.counts()[OutboxStatus.PENDING] == 2
    built = runtime.rebuild.execute(FakeBgeM3Embeddings().identity)
    drained = runtime.drain.execute()
    assert (built.revisions, drained.failed) == (2, 0)
    assert runtime.outbox.counts()[OutboxStatus.PENDING] == 0
    es = ElasticsearchSearchIndex(ElasticsearchClient(es_url))
    stored = {
        (d.article_id, d.content_hash)
        for d in PostgresSourceVersions(engine).current_documents()
    }
    assert {
        (d.article_id, d.content_hash) for d in es.revision_digests(built.generation)
    } == stored

    briefing = compose_briefing(
        PostgresRetrievalStore(engine),
        references=PostgresSourceReferences(engine),
        candidates=IndexedCandidateSource(
            index=es,
            embedder=FakeBgeM3Embeddings(),
            generations=PostgresSearchGenerations(engine).active,
        ),
        fallback_to_store=False,
    )
    outcome = briefing.use_case.execute(query(QUESTION))
    assert outcome.retrieval.provenance.search_backend == "elasticsearch"
    top = outcome.retrieval.articles[0]
    assert str(top.article_id) == records[0]["article_id"]
    view = briefing.sources.execute(outcome.cited_sources[0].source_ref, FIXTURE_CALLER)
    start, end = view.span.start_offset, view.span.end_offset
    assert view.span.excerpt == view.normalized_content[start:end]
    assert "\u200b" in view.normalized_content  # not cleaned on the way

    again = importer.execute(report, FIXTURE_CALLER)
    assert (again.revisions_inserted, again.provenance_inserted) == (0, 0)
    assert runtime.outbox.counts()[OutboxStatus.PENDING] == 0
    assert runtime.drain.execute().done == 0
    assert runtime.reconcile.execute(deep=True).consistent
