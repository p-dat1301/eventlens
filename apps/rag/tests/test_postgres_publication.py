from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import RaiseException

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.publication import PostgresPublication, Publication
from eventlens_rag.adapters.staging import IncompleteGenerationError, PostgresStaging
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest

type Factory = Callable[[str], ArticleDocument]
type Metadata = Callable[[ArticleDocument], ArticleEnrichment]


def stage(url: str, article: ArticleDocument) -> int:
    chunks = chunk_document(article)
    vectors = tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks)
    return PostgresStaging(url).stage(
        StageRequest(article, EmbeddingIdentity(), chunks, vectors)
    )


def snapshot(url: str, job: UUID, article: ArticleDocument) -> tuple[object, ...]:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            """SELECT job.status, job.claim_generation, current.active_index_generation,
                (SELECT count(*) FROM rag_enrichment_versions v WHERE v.job_id = %s)
            FROM rag_enrichment_jobs job
            JOIN rag_article_current current USING (article_id)
            WHERE job.job_id = %s""",
            (job, job),
        ).fetchone()
    assert row is not None
    assert row[0] is not None
    return (*row, article.article_id)


def expire(url: str, job: UUID) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE job_id = %s""",
            (job,),
        )


@contextmanager
def failing_trigger(url: str, table: str, condition: str) -> Generator[None]:
    """Install a temporary trigger that aborts the guarded write mid-transaction."""
    name = sql.Identifier(f"rag_test_fail_{table}")
    with psycopg.connect(url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL(
                """CREATE OR REPLACE FUNCTION {name}() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                IF {condition} THEN RAISE EXCEPTION 'injected interruption'; END IF;
                RETURN NEW; END $$"""
            ).format(name=name, condition=sql.SQL(condition))  # pyright: ignore[reportArgumentType]
        )
        _ = connection.execute(
            sql.SQL(
                """CREATE TRIGGER {name} BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION {name}()"""
            ).format(name=name, table=sql.Identifier(table))
        )
        try:
            yield
        finally:
            _ = connection.execute(
                sql.SQL("DROP TRIGGER {name} ON {table}").format(
                    name=name, table=sql.Identifier(table)
                )
            )


def test_publication_commits_ready_metadata_and_pointer_with_owner_fence(
    database_url: str, make_article: Factory, make_metadata: Metadata
) -> None:
    store = PostgresRagStore(database_url)
    article = make_article("Fenced publication article")
    store.persist_article(article)
    generation = stage(database_url, article)
    job = store.enqueue_job(article.article_id, article.content_hash, "v1", "v1", "v1")
    claim = store.claim_job(job)
    assert claim is not None
    publisher = PostgresPublication(database_url)
    metadata = make_metadata(article)
    assert not publisher.publish(Publication(job, claim + 1, generation, metadata))
    assert publisher.publish(Publication(job, claim, generation, metadata))
    assert snapshot(database_url, job, article)[:4] == ("READY", claim, generation, 1)
    assert store.claim_job(job) is None


def test_expired_lease_owner_cannot_publish_after_reclaim(
    database_url: str, make_article: Factory, make_metadata: Metadata
) -> None:
    store = PostgresRagStore(database_url)
    article = make_article("Lease fencing article")
    store.persist_article(article)
    generation = stage(database_url, article)
    job = store.enqueue_job(article.article_id, article.content_hash, "v1", "v1", "v1")
    worker_a = store.claim_job(job)
    assert worker_a is not None
    assert store.claim_job(job) is None
    publisher = PostgresPublication(database_url)
    metadata = make_metadata(article)
    expire(database_url, job)
    # Expiry without reclaim: A no longer owns the job.
    assert not publisher.publish(Publication(job, worker_a, generation, metadata))
    worker_b = store.claim_job(job)
    assert worker_b is not None
    assert worker_b == worker_a + 1
    before = snapshot(database_url, job, article)
    # A's late output after B's reclaim changes nothing.
    assert not publisher.publish(Publication(job, worker_a, generation, metadata))
    assert snapshot(database_url, job, article) == before
    assert before[:4] == ("PROCESSING", worker_b, None, 0)
    assert publisher.publish(Publication(job, worker_b, generation, metadata))


def test_concurrent_claims_have_exactly_one_winner(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(database_url)
    article = make_article("Concurrent claim article")
    store.persist_article(article)
    workers = 8
    barrier = Barrier(workers)

    def enqueue_then_claim(_: int) -> tuple[UUID, int | None]:
        _ = barrier.wait()
        job = store.enqueue_job(
            article.article_id, article.content_hash, "v1", "v1", "v1"
        )
        _ = barrier.wait()
        return job, store.claim_job(job)

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(enqueue_then_claim, range(workers)))
    assert len({job for job, _ in results}) == 1
    assert sorted(claim for _, claim in results if claim is not None) == [1]


def test_interrupted_publication_rolls_back_and_retry_succeeds(
    database_url: str, make_article: Factory, make_metadata: Metadata
) -> None:
    store = PostgresRagStore(database_url)
    article = make_article("Interrupted publication article")
    store.persist_article(article)
    generation = stage(database_url, article)
    job = store.enqueue_job(article.article_id, article.content_hash, "v1", "v1", "v1")
    claim = store.claim_job(job)
    assert claim is not None
    before = snapshot(database_url, job, article)
    request = Publication(job, claim, generation, make_metadata(article))
    publisher = PostgresPublication(database_url)
    with (
        failing_trigger(database_url, "rag_enrichment_versions", "TRUE"),
        pytest.raises(RaiseException),
    ):
        _ = publisher.publish(request)
    assert snapshot(database_url, job, article) == before
    assert publisher.publish(request)
    assert snapshot(database_url, job, article)[:4] == ("READY", claim, generation, 1)


def test_interrupted_staging_leaves_no_rows_and_retry_is_idempotent(
    database_url: str, make_article: Factory
) -> None:
    article = make_article("Long rebuild text. " * 140)
    assert len(chunk_document(article)) > 1
    rows = """SELECT (SELECT count(*) FROM rag_index_generations WHERE article_id = %s)
        + (SELECT count(*) FROM rag_chunks WHERE article_id = %s)"""
    PostgresRagStore(database_url).persist_article(article)
    condition = f"NEW.chunk_index = 1 AND NEW.article_id = '{article.article_id}'"
    with (
        failing_trigger(database_url, "rag_chunks", condition),
        pytest.raises(RaiseException),
    ):
        _ = stage(database_url, article)
    with psycopg.connect(database_url) as connection:
        count = connection.execute(
            rows.encode(), (article.article_id, article.article_id)
        ).fetchone()
    assert count == (0,)
    generation = stage(database_url, article)
    assert stage(database_url, article) == generation
    assert PostgresStaging(database_url).activate(generation)


def test_corpus_flip_is_all_or_nothing_when_a_generation_is_incomplete(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(database_url)
    first = make_article("Complete corpus member")
    second = make_article("Damaged corpus member")
    for article in (first, second):
        store.persist_article(article)
    generations = [stage(database_url, first), stage(database_url, second)]
    with psycopg.connect(database_url) as connection:
        _ = connection.execute(
            "DELETE FROM rag_chunks WHERE generation = %s", (generations[1],)
        )
    staging = PostgresStaging(database_url)
    assert not staging.activate(generations[1])
    with pytest.raises(IncompleteGenerationError):
        _ = staging.activate_all(generations)
    with psycopg.connect(database_url) as connection:
        pointers = connection.execute(
            """SELECT count(*) FROM rag_article_current
            WHERE article_id = ANY(%s) AND active_index_generation IS NOT NULL""",
            ([first.article_id, second.article_id],),
        ).fetchone()
    assert pointers == (0,)
    assert staging.activate_all(generations[:1]) == 1
