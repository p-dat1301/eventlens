"""PostgreSQL migration and T3 storage primitives."""

from pathlib import Path
from typing import final
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import scalar_row
from pydantic import TypeAdapter

from eventlens_rag.adapters.migrations import migrate
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.domain.article_document import ArticleDocument


@final
class PostgresRagStore:
    """Own atomic document, job, and active-generation writes."""

    def __init__(self, database_url: str) -> None:
        """Retain explicit connection configuration."""
        self._database_url = database_url

    def migrate(self, directory: Path) -> None:
        """Apply ordered, idempotent SQL migrations."""
        migrate(self._database_url, directory)

    def persist_article(self, document: ArticleDocument) -> None:
        """Persist immutable revision and make lexical current pointer immediately."""
        with (
            psycopg.connect(self._database_url) as connection,
            connection.cursor() as cursor,
        ):
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

    def tombstone_article(self, article_id: UUID) -> None:
        """Synchronously invalidate current lexical/vector visibility."""
        with psycopg.connect(self._database_url) as connection:
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
        """Insert one five-field idempotent enrichment job."""
        with psycopg.connect(self._database_url) as connection:
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
                ) DO UPDATE SET job_id = rag_enrichment_jobs.job_id
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
            return TypeAdapter(UUID).validate_python(row)

    def claim_job(self, job_id: UUID, lease_seconds: int = 30) -> int | None:
        """Claim job only when no valid lease exists, incrementing generation."""
        with psycopg.connect(self._database_url) as connection:
            row = (
                connection.cursor(row_factory=scalar_row)
                .execute(
                    """
                UPDATE rag_enrichment_jobs
                SET status = 'PROCESSING', claim_generation = claim_generation + 1,
                    lease_expires_at = now() + make_interval(secs => %s)
                WHERE job_id = %s
                  AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                  AND status <> 'READY'
                RETURNING claim_generation
                """,
                    (lease_seconds, job_id),
                )
                .fetchone()
            )
            adapter: TypeAdapter[int | None] = TypeAdapter(int | None)
            return adapter.validate_python(row)

    def complete_job(self, job_id: UUID, generation: int) -> bool:
        """Fence completion against current unexpired lease owner."""
        with psycopg.connect(self._database_url) as connection:
            result = connection.execute(
                """
                UPDATE rag_enrichment_jobs
                SET status = 'READY', lease_expires_at = NULL
                WHERE job_id = %s AND claim_generation = %s
                  AND lease_expires_at > clock_timestamp() AND status = 'PROCESSING'
                  AND EXISTS (SELECT 1 FROM rag_article_current current
                    WHERE current.article_id = rag_enrichment_jobs.article_id
                    AND current.content_hash = rag_enrichment_jobs.content_hash
                    AND current.tombstoned_at IS NULL)
                """,
                (job_id, generation),
            )
            return result.rowcount == 1

    def publish_generation(self, article_id: UUID, generation: int) -> bool:
        """Activate only a complete generation belonging to this current article."""
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """SELECT generation FROM rag_index_generations
                WHERE generation = %s AND article_id = %s""",
                (generation, article_id),
            ).fetchone()
        return row is not None and PostgresStaging(self._database_url).activate(
            generation
        )
