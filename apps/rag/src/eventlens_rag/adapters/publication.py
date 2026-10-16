"""Fenced metadata and vector publication storage primitive."""

from dataclasses import dataclass
from uuid import UUID

from eventlens_contracts import ArticleEnrichment, EnrichmentStatus
from eventlens_rag.adapters.connection import connect


@dataclass(frozen=True, slots=True)
class Publication:
    """Ownership proof and validated metadata for one staged generation."""

    job_id: UUID
    claim_generation: int
    index_generation: int
    metadata: ArticleEnrichment


@dataclass(frozen=True, slots=True)
class PostgresPublication:
    """Serialize current-content checks with job ownership and pointer update."""

    database_url: str

    def publish(self, request: Publication) -> bool:
        """Commit READY, metadata and active vector pointer together or not at all."""
        if request.metadata.status is not EnrichmentStatus.READY:
            return False
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT current.article_id
                FROM rag_article_current current
                JOIN rag_enrichment_jobs job USING (article_id, content_hash)
                JOIN rag_index_generations staged USING (article_id, content_hash)
                WHERE job.job_id = %s AND job.claim_generation = %s
                AND job.status = 'PROCESSING'
                AND current.tombstoned_at IS NULL
                AND staged.generation = %s AND staged.expected_chunks > 0
                AND staged.expected_chunks = (SELECT count(*) FROM rag_chunks
                    WHERE generation = staged.generation)
                AND job.article_id = %s AND job.content_hash = %s
                AND job.enrichment_version = %s AND job.model_version = %s
                AND job.prompt_version = %s
                FOR UPDATE OF current, job, staged""",
                (
                    request.job_id,
                    request.claim_generation,
                    request.index_generation,
                    request.metadata.article_id,
                    request.metadata.content_hash,
                    request.metadata.enrichment_version,
                    request.metadata.model_version,
                    request.metadata.prompt_version,
                ),
            ).fetchone()
            if row is None:
                return False
            changed = connection.execute(
                """UPDATE rag_enrichment_jobs
                SET status = 'READY', lease_expires_at = NULL
                WHERE job_id = %s AND claim_generation = %s
                AND status = 'PROCESSING' AND lease_expires_at > clock_timestamp()""",
                (request.job_id, request.claim_generation),
            ).rowcount
            if changed != 1:
                return False
            _ = connection.execute(
                """INSERT INTO rag_enrichment_versions (job_id, generation, metadata)
                VALUES (%s,%s,%s::jsonb)""",
                (
                    request.job_id,
                    request.index_generation,
                    request.metadata.model_dump_json(),
                ),
            )
            _ = connection.execute(
                """UPDATE rag_index_generations SET status = 'ACTIVE'
                WHERE generation = %s""",
                (request.index_generation,),
            )
            _ = connection.execute(
                """UPDATE rag_article_current SET active_index_generation = %s
                WHERE article_id = %s AND content_hash = %s""",
                (
                    request.index_generation,
                    request.metadata.article_id,
                    request.metadata.content_hash,
                ),
            )
            return True
