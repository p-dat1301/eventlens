"""PostgreSQL migration and T3 storage primitives."""

from pathlib import Path
from typing import final
from uuid import UUID, uuid4

from psycopg.rows import scalar_row
from pydantic import TypeAdapter

from eventlens_rag.adapters.connection import connect
from eventlens_rag.adapters.migrations import migrate
from eventlens_rag.domain.article_document import ArticleDocument


@final
class PostgresRagStore:
    """Own atomic document, job, and job-claim writes."""

    def __init__(self, database_url: str) -> None:
        """Retain explicit connection configuration."""
        self._database_url = database_url

    def migrate(self, directory: Path) -> None:
        """Apply ordered, idempotent SQL migrations."""
        migrate(self._database_url, directory)

    def persist_article(self, document: ArticleDocument) -> None:
        """Persist immutable revision and make lexical current pointer immediately.

        A new content hash clears the active vector pointer and marks enrichment
        jobs for older hashes STALE in the same transaction, so old metadata and
        vectors never join the corrected text.
        """
        with connect(self._database_url) as connection, connection.cursor() as cursor:
            _ = cursor.execute(
                """
                    INSERT INTO rag_article_documents (
                        article_id, content_hash, source_id, title, normalized_content,
                        source_published_at, document_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (article_id, content_hash) DO NOTHING
                    """,
                (
                    document.article_id,
                    document.content_hash,
                    document.source_id,
                    document.title,
                    document.normalized_content,
                    document.source_published_at,
                    document.model_dump_json(),
                ),
            )
            _ = cursor.execute(
                """
                    INSERT INTO rag_article_current (
                        article_id, content_hash, tombstoned_at)
                    VALUES (%s, %s, NULL)
                    ON CONFLICT (article_id) DO UPDATE
                    SET content_hash = EXCLUDED.content_hash,
                        active_index_generation = CASE
                            WHEN rag_article_current.content_hash
                                = EXCLUDED.content_hash
                            THEN rag_article_current.active_index_generation
                            ELSE NULL END
                    WHERE rag_article_current.tombstoned_at IS NULL
                    """,
                (document.article_id, document.content_hash),
            )
            _ = cursor.execute(
                """
                    UPDATE rag_enrichment_jobs
                    SET status = 'STALE', lease_expires_at = NULL
                    WHERE article_id = %s AND content_hash <> %s
                      AND status <> 'STALE'
                    """,
                (document.article_id, document.content_hash),
            )

    def tombstone_article(self, article_id: UUID) -> None:
        """Synchronously invalidate current lexical/vector visibility."""
        with connect(self._database_url) as connection:
            _ = connection.execute(
                """
                UPDATE rag_article_current
                SET tombstoned_at = now(), active_index_generation = NULL
                WHERE article_id = %s
                """,
                (article_id,),
            )

    def enqueue_job(
        self,
        article_id: UUID,
        content_hash: str,
        enrichment_version: str,
        model_version: str,
        prompt_version: str,
    ) -> UUID:
        """Insert or requeue a stale job only when its revision is current again."""
        with connect(self._database_url) as connection:
            _ = connection.execute(
                """SELECT article_id FROM rag_article_current
                WHERE article_id = %s FOR UPDATE""",
                (article_id,),
            ).fetchone()
            row = (
                connection.cursor(row_factory=scalar_row)
                .execute(
                    """
                INSERT INTO rag_enrichment_jobs (
                    job_id, article_id, content_hash, enrichment_version, model_version,
                    prompt_version
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    article_id, content_hash, enrichment_version,
                    model_version, prompt_version
                ) DO UPDATE SET status = CASE
                    WHEN rag_enrichment_jobs.status = 'STALE'
                     AND EXISTS (SELECT 1 FROM rag_article_current current
                         WHERE current.article_id = EXCLUDED.article_id
                           AND current.content_hash = EXCLUDED.content_hash
                           AND current.tombstoned_at IS NULL)
                    THEN 'PENDING' ELSE rag_enrichment_jobs.status END
                RETURNING job_id
                """,
                    (
                        uuid4(),
                        article_id,
                        content_hash,
                        enrichment_version,
                        model_version,
                        prompt_version,
                    ),
                )
                .fetchone()
            )
            job_id = TypeAdapter(UUID).validate_python(row)
            _ = connection.execute(
                """DELETE FROM rag_enrichment_versions
                WHERE job_id = %s AND EXISTS (
                    SELECT 1 FROM rag_enrichment_jobs
                    WHERE job_id = %s AND status = 'PENDING')""",
                (job_id, job_id),
            )
            return job_id

    def claim_job(self, job_id: UUID, lease_seconds: int = 30) -> int | None:
        """Claim a pending or lease-expired job, incrementing its fence generation.

        The single conditional UPDATE is the transaction-safe claim: concurrent
        callers serialize on the row lock and only one sees a matching predicate.
        """
        with connect(self._database_url) as connection:
            row = (
                connection.cursor(row_factory=scalar_row)
                .execute(
                    """
                UPDATE rag_enrichment_jobs
                SET status = 'PROCESSING', claim_generation = claim_generation + 1,
                    lease_expires_at = clock_timestamp()
                        + make_interval(secs => %s)
                WHERE job_id = %s
                  AND status IN ('PENDING', 'PROCESSING')
                  AND (lease_expires_at IS NULL
                       OR lease_expires_at <= clock_timestamp())
                RETURNING claim_generation
                """,
                    (lease_seconds, job_id),
                )
                .fetchone()
            )
            adapter: TypeAdapter[int | None] = TypeAdapter(int | None)
            return adapter.validate_python(row)
