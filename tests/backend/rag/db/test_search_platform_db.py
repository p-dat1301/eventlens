"""Search platform on real PostgreSQL: outbox triggers, fencing, generations,
embedding cache, projection source and the full PG -> outbox -> index flow."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import NoReturn, cast
from uuid import UUID

import psycopg
import pytest

from backend.rag.application.use_cases.search_candidates import IndexedCandidateSource
from backend.rag.composition import (
    SearchRuntime,
    SearchSettings,
    build_search_runtime,
    compose_briefing,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.outbox import OutboxEvent, OutboxStatus
from backend.rag.domain.paragraph_chunking import PARAGRAPH_CHUNKER_VERSION
from backend.rag.domain.search_embedding import (
    FAKE_BGE_M3,
    EmbeddingDimensionMismatchError,
    SearchEmbeddingIdentity,
)
from backend.rag.domain.search_index import GenerationStatus, text_hash
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.postgres.engine import rag_engine, transaction
from backend.rag.infra.postgres.search_platform import (
    GenerationStateError,
    PostgresEmbeddingCache,
    PostgresOutboxStore,
    PostgresProjectionSource,
    PostgresSearchGenerations,
)
from backend.rag.infra.postgres.store import PostgresRagStore, persist_revision
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.search.memory_index import InMemorySearchIndex
from briefing_support import query

type Factory = Callable[[str], ArticleDocument]
BODY = "\n\n".join(
    [
        (
            f"Đoạn {n}: Bộ Giao thông Vận tải công bố tiến độ "
            f"cao tốc Bắc Nam đạt {n * 9}%. Nhà thầu cam kết hoàn thành đúng hạn."
        )
        for n in range(8)
    ]
)


def _events(url: str) -> list[tuple[str, str | None]]:
    with psycopg.connect(url) as connection:
        rows = cast(
            "list[tuple[object, object]]",
            connection.execute(
                "SELECT event_type, article_id FROM rag_search_outbox ORDER BY event_id"
            ).fetchall(),
        )
        return [
            (str(kind), None if article is None else str(article))
            for kind, article in rows
        ]


def test_outbox_events_commit_with_the_data_and_roll_back_with_it(
    retrieval_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(retrieval_url))
    article = make_article(BODY)

    # A failed transaction leaves neither the revision nor its event.
    def abort() -> NoReturn:
        with transaction(rag_engine(retrieval_url)) as conn:
            persist_revision(conn, article)
            msg = "abort after the write"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        abort()
    assert _events(retrieval_url) == []

    store.persist_article(article)
    store.persist_article(article)  # same hash: no event
    corrected = make_article(BODY + " Đính chính.").model_copy(
        update={"article_id": article.article_id}
    )
    store.persist_article(corrected)
    store.tombstone_article(article.article_id)

    assert _events(retrieval_url) == [
        ("article_revision_created", str(article.article_id)),
        ("article_revision_updated", str(article.article_id)),
        ("article_tombstoned", str(article.article_id)),
    ]


def test_claims_are_disjoint_and_fenced_by_claim_token(
    retrieval_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(retrieval_url))
    for n in range(20):
        store.persist_article(make_article(f"{BODY} bài {n}"))
    outbox = PostgresOutboxStore(rag_engine(retrieval_url))

    def claim(_: int) -> tuple[OutboxEvent, ...]:
        return outbox.claim(5, 60)

    with ThreadPoolExecutor(4) as pool:
        batches = list(pool.map(claim, range(4)))
    ids = [event.event_id for batch in batches for event in batch]
    assert len(ids) == len(set(ids)) == 20

    first = batches[0][0]
    with psycopg.connect(retrieval_url) as connection:
        _ = connection.execute(
            """UPDATE rag_search_outbox
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE event_id = %s""",
            (first.event_id,),
        )
    (reclaimed,) = [e for e in outbox.claim(50, 60) if e.event_id == first.event_id]
    assert reclaimed.claim_token != first.claim_token
    assert reclaimed.attempts == 2
    assert outbox.complete([first]) == 0  # stale owner cannot ack
    assert not outbox.retry(first, "x", 1)
    assert outbox.complete([reclaimed]) == 1


def test_retry_dead_letter_requeue_and_counts(
    retrieval_url: str, make_article: Factory
) -> None:
    PostgresRagStore(rag_engine(retrieval_url)).persist_article(make_article(BODY))
    outbox = PostgresOutboxStore(rag_engine(retrieval_url))
    (event,) = outbox.claim(10, 60)
    assert outbox.retry(event, "search_index_unavailable", 3600)
    assert outbox.claim(10, 60) == ()  # backoff holds it
    assert outbox.counts()[OutboxStatus.PENDING] == 1
    with psycopg.connect(retrieval_url) as connection:
        _ = connection.execute(
            "UPDATE rag_search_outbox SET next_attempt_at = clock_timestamp()"
        )
    (again,) = outbox.claim(10, 60)
    assert outbox.fail(again, "search_index_unavailable")
    assert outbox.counts()[OutboxStatus.FAILED] == 1
    assert outbox.requeue_failed() == 1
    assert outbox.oldest_pending() is not None


def test_generation_lifecycle_keeps_one_active_and_emits_activation(
    retrieval_url: str,
) -> None:
    generations = PostgresSearchGenerations(rag_engine(retrieval_url))
    first = generations.create(
        PARAGRAPH_CHUNKER_VERSION, FAKE_BGE_M3, "eventlens-rag-search"
    )
    assert first.status is GenerationStatus.BUILDING
    # Generation IDs restart per database: a random suffix keeps two databases
    # sharing one cluster from ever reusing (and mixing into) the same index.
    assert first.index_name.startswith(f"eventlens-rag-search-g{first.generation_id}-")
    again = generations.create(PARAGRAPH_CHUNKER_VERSION, None, "eventlens-rag-search")
    assert again.index_name != first.index_name
    generations.mark_failed(again.generation_id)
    active = generations.activate(first.generation_id)
    second = generations.create(PARAGRAPH_CHUNKER_VERSION, None, "eventlens-rag-search")
    assert {g.generation_id for g in generations.writable()} == {
        first.generation_id,
        second.generation_id,
    }
    _ = generations.activate(second.generation_id)

    current = generations.active()
    previous = generations.get(first.generation_id)
    assert current is not None
    assert previous is not None
    assert current.generation_id == second.generation_id
    assert previous.status is GenerationStatus.RETIRED
    assert active.embedding_dimensions == 1024
    with pytest.raises(GenerationStateError):
        _ = generations.activate(first.generation_id)
    third = generations.create(PARAGRAPH_CHUNKER_VERSION, None, "eventlens-rag-search")
    generations.mark_failed(third.generation_id)
    with pytest.raises(GenerationStateError):
        _ = generations.activate(third.generation_id)
    kinds = [kind for kind, _ in _events(retrieval_url)]
    assert kinds.count("embedding_generation_activated") == 2


def test_embedding_cache_is_keyed_by_identity_and_dimension_checked(
    retrieval_url: str,
) -> None:
    cache = PostgresEmbeddingCache(rag_engine(retrieval_url))
    key = text_hash("Hà Nội")
    vector = FakeBgeM3Embeddings().embed_query("Hà Nội")
    assert cache.put_many([(key, vector)], FAKE_BGE_M3) == 1
    assert cache.put_many([(key, vector)], FAKE_BGE_M3) == 0  # idempotent
    assert cache.get_many([key], FAKE_BGE_M3) == {key: vector}
    other = SearchEmbeddingIdentity("bge-m3-1024.v1", "BAAI/bge-m3", 1024)
    assert cache.get_many([key], other) == {}
    with pytest.raises(EmbeddingDimensionMismatchError):
        _ = cache.put_many([(key, vector[:768])], other)


def test_projection_source_reads_only_current_untombstoned_revisions(
    retrieval_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(retrieval_url))
    articles = [make_article(f"{BODY} {n}") for n in range(5)]
    for article in articles:
        store.persist_article(article)
    store.tombstone_article(articles[0].article_id)
    source = PostgresProjectionSource(rag_engine(retrieval_url))

    page = source.current_page(None, 2) + source.current_page(
        source.current_page(None, 2)[-1].article_id, 10
    )
    assert {p.article_id for p in page} == {a.article_id for a in articles[1:]}
    assert source.revision(articles[0].article_id).tombstoned
    unknown = UUID("00000000-0000-4000-8000-00000000dead")
    assert source.revision(unknown).document is None
    assert len(source.current_digests()) == 4


def _runtime(url: str, index: InMemorySearchIndex) -> SearchRuntime:
    settings = SearchSettings.from_environment(
        {
            "EVENTLENS_RAG_SEARCH_BACKEND": "memory",
            "EVENTLENS_RAG_SEARCH_EMBEDDINGS": "fake-bge-m3",
        }
    )
    return build_search_runtime(url, settings, index=index)


def test_postgres_to_outbox_to_index_flow_and_full_rebuild(
    retrieval_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(retrieval_url))
    index = InMemorySearchIndex()
    runtime = _runtime(retrieval_url, index)
    keep, change, drop = (make_article(f"{BODY} {tag}") for tag in "ABC")
    for article in (keep, change, drop):
        store.persist_article(article)

    assert runtime.embedder is not None
    built = runtime.rebuild.execute(runtime.embedder.identity)
    corrected = make_article(f"{BODY} B đã sửa").model_copy(
        update={"article_id": change.article_id}
    )
    store.persist_article(corrected)
    store.tombstone_article(drop.article_id)
    drained = runtime.drain.execute()

    assert drained.failed == 0
    report = runtime.reconcile.execute(deep=True)
    assert report.consistent, report
    digests = {
        (d.article_id, d.content_hash) for d in index.revision_digests(built.generation)
    }
    assert digests == {
        (keep.article_id, keep.content_hash),
        (change.article_id, corrected.content_hash),
    }
    counts = runtime.outbox.counts()
    assert counts[OutboxStatus.PENDING] == counts[OutboxStatus.FAILED] == 0

    # The index is fully derived: drop it and rebuild from PostgreSQL alone.
    index.indices.clear()
    rebuilt = runtime.rebuild.execute(runtime.embedder.identity)
    assert {
        (d.article_id, d.content_hash)
        for d in index.revision_digests(rebuilt.generation)
    } == digests
    assert rebuilt.embedded == 0  # every vector came from the PostgreSQL cache


def test_api_falls_back_to_postgres_when_the_index_is_down(
    retrieval_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(retrieval_url))
    article = make_article(BODY)
    store.persist_article(article)
    index = InMemorySearchIndex()
    runtime = _runtime(retrieval_url, index)
    assert runtime.embedder is not None
    _ = runtime.rebuild.execute(runtime.embedder.identity)
    candidates = IndexedCandidateSource(
        index=index,
        embedder=FakeBgeM3Embeddings(),
        generations=PostgresSearchGenerations(rag_engine(retrieval_url)).active,
    )
    briefing = compose_briefing(
        PostgresRetrievalStore(rag_engine(retrieval_url)), candidates=candidates
    )

    served = briefing.use_case.execute(query("tiến độ cao tốc Bắc Nam"))
    index.unavailable = True
    fallback = briefing.use_case.execute(query("tiến độ cao tốc Bắc Nam"))

    assert served.retrieval.provenance.search_backend == "memory"
    assert fallback.retrieval.provenance.search_backend == "postgres_fallback"
    assert served.retrieval.articles[0].article_id == article.article_id
    assert fallback.retrieval.articles[0].article_id == article.article_id
