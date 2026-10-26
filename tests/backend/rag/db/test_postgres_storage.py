from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import RaiseException, UniqueViolation
from psycopg.rows import scalar_row

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import chunk_document
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.staging import StageRequest
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.migrations import MigrationChangedError, migrate
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore

type Factory = Callable[[str], ArticleDocument]
MIGRATIONS = Path(__file__).parents[4] / "src/backend/rag/infra/postgres/migrations"


def stage(
    url: str, article: ArticleDocument, identity: EmbeddingIdentity | None = None
) -> int:
    chunks = chunk_document(article)
    vectors = tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks)
    return PostgresStaging(rag_engine(url)).stage(
        StageRequest(article, identity or EmbeddingIdentity(), chunks, vectors)
    )


def scalar(url: str, query: str, params: tuple[object, ...]) -> object:
    with (
        psycopg.connect(url) as connection,
        connection.cursor(row_factory=scalar_row) as cursor,
    ):
        _ = cursor.execute(query.encode(), params)
        return cursor.fetchone()


def test_applied_migration_checksum_rejects_modified_source(
    database_url: str, tmp_path: Path
) -> None:
    _ = (tmp_path / "001_enable_vector.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationChangedError):
        migrate(database_url, tmp_path)


def test_database_enforces_immutable_revisions(
    database_url: str, make_article: Factory
) -> None:
    article = make_article("Immutable revision")
    PostgresRagStore(rag_engine(database_url)).persist_article(article)
    with psycopg.connect(database_url) as connection, pytest.raises(RaiseException):
        _ = connection.execute(
            "UPDATE rag_article_documents SET title = 'mutated' WHERE article_id = %s",
            (article.article_id,),
        )


def test_duplicate_document_delivery_is_suppressed(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(database_url))
    article = make_article("Duplicate delivery text")
    store.persist_article(article)
    store.persist_article(article)
    count = "SELECT count(*) FROM rag_article_documents WHERE article_id = %s"
    assert scalar(database_url, count, (article.article_id,)) == 1


def test_five_field_key_is_unique_and_each_field_distinguishes_jobs(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(database_url))
    article = make_article("Five field identity")
    store.persist_article(article)
    key = (article.article_id, article.content_hash)
    first = store.enqueue_job(*key, "e1", "m1", "p1")
    assert store.enqueue_job(*key, "e1", "m1", "p1") == first
    distinct = {
        store.enqueue_job(*key, "e2", "m1", "p1"),
        store.enqueue_job(*key, "e1", "m2", "p1"),
        store.enqueue_job(*key, "e1", "m1", "p2"),
    }
    assert first not in distinct
    assert len(distinct) == 3
    with psycopg.connect(database_url) as connection, pytest.raises(UniqueViolation):
        _ = connection.execute(
            """INSERT INTO rag_enrichment_jobs (job_id, article_id, content_hash,
            enrichment_version, model_version, prompt_version)
            VALUES (%s, %s, %s, 'e1', 'm1', 'p1')""",
            (uuid4(), *key),
        )


def test_real_vector_sql_preserves_baseline_dimension(database_url: str) -> None:
    values = "[" + ",".join(["1"] + ["0"] * 1535) + "]"
    dims = "SELECT vector_dims(%s::vector(1536))"
    assert scalar(database_url, dims, (values,)) == 1536
    distance = "SELECT %s::vector(1536) <=> %s::vector(1536)"
    assert scalar(database_url, distance, (values, values)) == 0


def test_lexical_first_then_hnsw_query_exposes_index_metadata(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(database_url))
    article = make_article("Harbor flooding closes the eastern ferry terminal")
    store.persist_article(article)
    fts = """SELECT count(*) FROM rag_current_lexical WHERE article_id = %s
        AND search_vector @@ plainto_tsquery('simple', 'ferry')"""
    assert scalar(database_url, fts, (article.article_id,)) == 1
    vectors = "SELECT count(*) FROM rag_current_vectors WHERE article_id = %s"
    staging = PostgresStaging(rag_engine(database_url))
    generation = stage(database_url, article)
    assert stage(database_url, article) == generation
    assert scalar(database_url, vectors, (article.article_id,)) == 0
    assert staging.activate(generation)
    query = FakeEmbeddings().embed(article.normalized_content).values
    literal = "[" + ",".join(map(str, query)) + "]"
    with psycopg.connect(database_url) as connection:
        _ = connection.execute("SET LOCAL enable_seqscan = off")
        plan = connection.execute(
            """EXPLAIN SELECT chunk_id FROM rag_chunks
            ORDER BY embedding <=> %s::vector(1536) LIMIT 1""",
            (literal,),
        ).fetchall()
        row = connection.execute(
            """SELECT vec.article_id, vec.content_hash, vec.start_offset,
                vec.end_offset, meta.corpus_generation, meta.chunker_version,
                meta.embedding_model, meta.indexed_at IS NOT NULL
            FROM rag_current_vectors vec
            JOIN rag_index_generations meta USING (generation)
            ORDER BY vec.embedding <=> %s::vector(1536) LIMIT 1""",
            (literal,),
        ).fetchone()
    assert "rag_chunks_embedding_hnsw_idx" in str(plan)
    assert row == (
        article.article_id,
        article.content_hash,
        0,
        len(article.normalized_content),
        "rag-fixture-corpus.v1",
        "codepoint-1200-200.v1",
        "fixture-embedding",
        True,
    )


def test_correction_during_rebuild_hides_old_vectors_and_stales_jobs(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(database_url))
    old = make_article("Original bridge report")
    article_id = old.article_id
    store.persist_article(old)
    old_job = store.enqueue_job(old.article_id, old.content_hash, "v1", "v1", "v1")
    old_generation = stage(database_url, old)
    assert PostgresStaging(rag_engine(database_url)).activate(old_generation)
    rebuild = EmbeddingIdentity(corpus_generation="rag-fixture-corpus.v2")
    pending_generation = stage(database_url, old, rebuild)
    corrected = make_article("Corrected tunnel report").model_copy(
        update={"article_id": article_id}
    )
    store.persist_article(corrected)
    lexical = """SELECT count(*) FROM rag_current_lexical WHERE article_id = %s
        AND search_vector @@ plainto_tsquery('simple', %s)"""
    assert scalar(database_url, lexical, (article_id, "bridge")) == 0
    assert scalar(database_url, lexical, (article_id, "tunnel")) == 1
    vectors = "SELECT count(*) FROM rag_current_vectors WHERE article_id = %s"
    assert scalar(database_url, vectors, (article_id,)) == 0
    status = "SELECT status FROM rag_enrichment_jobs WHERE job_id = %s"
    assert scalar(database_url, status, (old_job,)) == "STALE"
    assert store.claim_job(old_job) is None
    staging = PostgresStaging(rag_engine(database_url))
    assert staging.activate_all([old_generation, pending_generation]) == 0
    assert scalar(database_url, vectors, (article_id,)) == 0


def test_tombstone_is_synchronous_and_survives_stale_replay(
    database_url: str, make_article: Factory
) -> None:
    store = PostgresRagStore(rag_engine(database_url))
    article = make_article("Tombstoned article text")
    store.persist_article(article)
    generation = stage(database_url, article)
    staging = PostgresStaging(rag_engine(database_url))
    assert staging.activate(generation)
    store.tombstone_article(article.article_id)
    lexical = "SELECT count(*) FROM rag_current_lexical WHERE article_id = %s"
    vectors = "SELECT count(*) FROM rag_current_vectors WHERE article_id = %s"
    assert scalar(database_url, lexical, (article.article_id,)) == 0
    assert scalar(database_url, vectors, (article.article_id,)) == 0
    assert not staging.activate(generation)
    store.persist_article(article)
    assert scalar(database_url, lexical, (article.article_id,)) == 0


def test_unknown_article_cannot_be_activated(database_url: str) -> None:
    assert PostgresStaging(rag_engine(database_url)).activate_all([-1]) == 0
    missing = "SELECT count(*) FROM rag_article_current WHERE article_id = %s"
    assert scalar(database_url, missing, (UUID(int=0),)) == 0
