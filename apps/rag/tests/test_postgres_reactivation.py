from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.publication import PostgresPublication, Publication
from eventlens_rag.adapters.staging import (
    DuplicateArticleGenerationError,
    PostgresStaging,
)
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest

type Factory = Callable[[str], ArticleDocument]
type Metadata = Callable[[ArticleDocument], ArticleEnrichment]


def stage(url: str, article: ArticleDocument, identity: EmbeddingIdentity) -> int:
    chunks = chunk_document(article)
    vectors = tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks)
    return PostgresStaging(url).stage(StageRequest(article, identity, chunks, vectors))


def snapshot(url: str, job: UUID) -> tuple[str, int, int | None, int]:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            """SELECT job.status, job.claim_generation,
                current.active_index_generation,
                (SELECT count(*) FROM rag_enrichment_versions WHERE job_id = %s)
            FROM rag_enrichment_jobs job
            JOIN rag_article_current current USING (article_id)
            WHERE job.job_id = %s""",
            (job, job),
        ).fetchone()
    assert row is not None
    return row


def test_reenqueue_stale_job_after_a_b_a_fences_old_owner(
    database_url: str, make_article: Factory, make_metadata: Metadata
) -> None:
    store = PostgresRagStore(database_url)
    original = make_article("Original report")
    correction = make_article("Corrected report").model_copy(
        update={"article_id": original.article_id}
    )
    store.persist_article(original)
    generation = stage(database_url, original, EmbeddingIdentity())
    job = store.enqueue_job(
        original.article_id, original.content_hash, "v1", "v1", "v1"
    )
    first_claim = store.claim_job(job)
    assert first_claim is not None
    store.persist_article(correction)
    assert (
        store.enqueue_job(original.article_id, original.content_hash, "v1", "v1", "v1")
        == job
    )
    assert store.claim_job(job) is None
    store.persist_article(original)

    assert (
        store.enqueue_job(original.article_id, original.content_hash, "v1", "v1", "v1")
        == job
    )
    second_claim = store.claim_job(job)
    assert second_claim == first_claim + 1
    assert second_claim is not None
    publisher = PostgresPublication(database_url)
    metadata = make_metadata(original)
    assert not publisher.publish(Publication(job, first_claim, generation, metadata))
    assert publisher.publish(Publication(job, second_claim, generation, metadata))


def test_reenqueue_stale_ready_job_replaces_old_publication(
    database_url: str, make_article: Factory, make_metadata: Metadata
) -> None:
    store = PostgresRagStore(database_url)
    original = make_article("Published original report")
    correction = make_article("Published corrected report").model_copy(
        update={"article_id": original.article_id}
    )
    store.persist_article(original)
    generation = stage(database_url, original, EmbeddingIdentity())
    job = store.enqueue_job(
        original.article_id, original.content_hash, "v1", "v1", "v1"
    )
    first_claim = store.claim_job(job)
    assert first_claim is not None
    publisher = PostgresPublication(database_url)
    metadata = make_metadata(original)
    assert publisher.publish(Publication(job, first_claim, generation, metadata))
    store.persist_article(correction)
    store.persist_article(original)

    assert (
        store.enqueue_job(original.article_id, original.content_hash, "v1", "v1", "v1")
        == job
    )
    assert snapshot(database_url, job) == ("PENDING", first_claim, None, 0)
    second_claim = store.claim_job(job)
    assert second_claim == first_claim + 1
    assert second_claim is not None
    assert publisher.publish(Publication(job, second_claim, generation, metadata))
    assert snapshot(database_url, job) == ("READY", second_claim, generation, 1)


def test_batch_rejects_distinct_generations_of_same_article_atomically(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(database_url)
    article = make_article("Two generations of same article")
    store.persist_article(article)
    original = stage(database_url, article, EmbeddingIdentity())
    other_article = make_article("Other batch article")
    store.persist_article(other_article)
    other = stage(database_url, other_article, EmbeddingIdentity())
    alternate = stage(
        database_url,
        article,
        EmbeddingIdentity(corpus_generation="other-corpus.v1"),
    )
    staging = PostgresStaging(database_url)
    with pytest.raises(DuplicateArticleGenerationError):
        _ = staging.activate_all([other, original, alternate])
    with psycopg.connect(database_url) as connection:
        statuses = connection.execute(
            "SELECT status FROM rag_index_generations WHERE generation = ANY(%s)",
            ([original, alternate, other],),
        ).fetchall()
        pointer = connection.execute(
            """SELECT active_index_generation FROM rag_article_current
            WHERE article_id = %s""",
            (article.article_id,),
        ).fetchone()
    assert statuses == [("STAGING",)] * 3
    assert pointer == (None,)
    assert staging.activate_all([original, original]) == 1
    assert staging.activate_all([original, other]) == 2
