import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import scalar_row

from eventlens_contracts import ArticleEnrichment, EnrichmentStatus
from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.postgres import PostgresRagStore
from eventlens_rag.adapters.publication import PostgresPublication, Publication
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest


def test_publication_commits_ready_metadata_and_pointer_with_owner_fence() -> None:
    url = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if url is None:
        pytest.skip("real PostgreSQL DSN required")
    root = Path(__file__).parents[3]
    source = ArticleDocument.model_validate_json(
        (root / "fixtures/rag_article_document.v1.json").read_text(encoding="utf-8")
    )
    article = source.model_copy(update={"article_id": uuid4(), "evidence_spans": ()})
    store = PostgresRagStore(url)
    store.migrate(root / "apps/rag/migrations")
    store.persist_article(article)
    chunks = chunk_document(article)
    generation = PostgresStaging(url).stage(
        StageRequest(
            article,
            EmbeddingIdentity(),
            chunks,
            tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks),
        )
    )
    job = store.enqueue_job(article.article_id, article.content_hash, "v1", "v1", "v1")
    claim = store.claim_job(job)
    assert claim is not None
    metadata = ArticleEnrichment(
        schema_version="article_enrichment.v1",
        article_id=article.article_id,
        content_hash=article.content_hash,
        enrichment_version="v1",
        model_version="v1",
        prompt_version="v1",
        status=EnrichmentStatus.READY,
        topics=(),
        content_type=None,
        event_relevance=None,
        entities=(),
        locations=(),
        suggested_tags=(),
        evidence_spans=(),
        field_confidence=(),
        enriched_at=datetime(2026, 9, 29, tzinfo=UTC),
        attempt_count=1,
        failure_code=None,
    )
    publisher = PostgresPublication(url)
    assert not publisher.publish(Publication(job, claim + 1, generation, metadata))
    assert publisher.publish(Publication(job, claim, generation, metadata))
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute(
            """SELECT count(*) FROM rag_enrichment_versions version
            JOIN rag_enrichment_jobs job USING (job_id)
            JOIN rag_article_current current USING (article_id, content_hash)
            WHERE job.job_id = %s AND job.status = 'READY'
            AND current.active_index_generation = version.generation""",
            (job,),
        )
        assert cursor.fetchone() == 1
