import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import RaiseException
from psycopg.rows import scalar_row

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.migrations import MigrationChangedError
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest


def database_url() -> str:
    value = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if value is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    return value


def document() -> ArticleDocument:
    content = "PostgreSQL lexical article"
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": UUID("00000000-0000-4000-8000-000000000011"),
            "source_id": UUID("00000000-0000-4000-8000-000000000012"),
            "title": "Database article",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://example.test/database",
            "source_name": "Example",
            "source_domain": "example.test",
            "source_published_at": None,
            "ingested_at": datetime(2026, 9, 29, tzinfo=UTC),
            "processed_at": datetime(2026, 9, 29, tzinfo=UTC),
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


def test_postgres_storage_uses_immutable_revision_jobs_and_tombstones() -> None:
    # Given
    store = PostgresRagStore(database_url())
    migrations = Path(__file__).parents[1] / "migrations"
    article = document()
    store.migrate(migrations)

    # When
    store.persist_article(article)
    job_id = store.enqueue_job(
        article.article_id,
        article.content_hash,
        "enrichment.v1",
        "model.v1",
        "prompt.v1",
    )
    first_claim = store.claim_job(job_id)
    assert first_claim is not None
    store.tombstone_article(article.article_id)

    # Then
    assert store.complete_job(job_id, first_claim) is False
    assert store.publish_generation(article.article_id, 1) is False


def test_applied_migration_checksum_rejects_modified_source(tmp_path: Path) -> None:
    store = PostgresRagStore(database_url())
    migrations = Path(__file__).parents[1] / "migrations"
    store.migrate(migrations)
    source = migrations / "001_enable_vector.sql"
    _ = (tmp_path / source.name).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationChangedError):
        store.migrate(tmp_path)


def test_database_enforces_immutable_revisions() -> None:
    url = database_url()
    store = PostgresRagStore(url)
    store.migrate(Path(__file__).parents[1] / "migrations")
    store.persist_article(document())
    with psycopg.connect(url) as connection, pytest.raises(RaiseException):
        _ = connection.execute("UPDATE rag_article_documents SET title = 'mutated'")


def test_real_vector_sql_preserves_baseline_dimension() -> None:
    url = database_url()
    PostgresRagStore(url).migrate(Path(__file__).parents[1] / "migrations")
    values = "[" + ",".join(["1"] + ["0"] * 1535) + "]"
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute("SELECT vector_dims(%s::vector(1536))", (values,))
        assert cursor.fetchone() == 1536
        _ = cursor.execute(
            "SELECT %s::vector(1536) <=> %s::vector(1536)", (values, values)
        )
        assert cursor.fetchone() == 0


def test_staging_retry_keeps_lexical_visible_then_atomically_activates() -> None:
    url = database_url()
    store = PostgresRagStore(url)
    store.migrate(Path(__file__).parents[1] / "migrations")
    article = document().model_copy(update={"article_id": uuid4()})
    store.persist_article(article)
    chunks = chunk_document(article)
    request = StageRequest(
        article,
        EmbeddingIdentity(),
        chunks,
        tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks),
    )
    staging = PostgresStaging(url)
    generation = staging.stage(request)
    assert staging.stage(request) == generation
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute(
            """SELECT count(*) FROM rag_current_lexical
                WHERE article_id = %s
                AND search_vector @@ plainto_tsquery('simple', 'lexical')""",
            (article.article_id,),
        )
        assert cursor.fetchone() == 1
        _ = cursor.execute(
            "SELECT count(*) FROM rag_current_vectors WHERE article_id = %s",
            (article.article_id,),
        )
        assert cursor.fetchone() == 0
    assert staging.activate(generation)
    store.tombstone_article(article.article_id)
    assert not staging.activate(generation)
    store.persist_article(article)
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute(
            "SELECT count(*) FROM rag_current_lexical WHERE article_id = %s",
            (article.article_id,),
        )
        assert cursor.fetchone() == 0


def test_expired_claim_cannot_complete_after_reclaim() -> None:
    url = database_url()
    store = PostgresRagStore(url)
    store.migrate(Path(__file__).parents[1] / "migrations")
    article = document().model_copy(update={"article_id": uuid4()})
    store.persist_article(article)
    job = store.enqueue_job(article.article_id, article.content_hash, "v1", "v1", "v1")
    first = store.claim_job(job)
    assert first is not None
    assert store.claim_job(job) is None
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs SET lease_expires_at = clock_timestamp()
            - interval '1 second' WHERE job_id = %s""",
            (job,),
        )
    assert not store.complete_job(job, first)
    second = store.claim_job(job)
    assert second is not None
    assert second > first
    assert not store.complete_job(job, first)
    assert store.complete_job(job, second)
