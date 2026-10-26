"""Fenced metadata and vector publication storage primitive."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from backend.rag.domain.enrichment_lifecycle import PublishResult
from backend.rag.infra.postgres.connection import connect
from backend.shared.contracts import ArticleEnrichment, EnrichmentStatus

type LockedHook = Callable[[], None]


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
        return self.publish_fenced(request) is PublishResult.PUBLISHED

    def publish_fenced(
        self, request: Publication, after_lock: LockedHook | None = None
    ) -> PublishResult:
        """Fenced atomic publication; cancellation is re-checked under row lock.

        The job, current-article and staged-generation rows are locked first. A
        cancellation recorded for this claim generation before the lock was
        granted aborts with ``CANCELLED``; one requested after this transaction
        commits finds the job READY and cannot undo it. ``after_lock`` is a test
        hook that runs while the locks are held.
        """
        if request.metadata.status is not EnrichmentStatus.READY:
            return PublishResult.LOST_OWNERSHIP
        with connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT job.cancel_requested_generation IS NOT DISTINCT FROM
                    job.claim_generation
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
            if after_lock is not None:
                after_lock()
            if row is None:
                return PublishResult.LOST_OWNERSHIP
            if row[0]:
                return PublishResult.CANCELLED
            changed = connection.execute(
                """UPDATE rag_enrichment_jobs
                SET status = 'READY', lease_expires_at = NULL, failure_code = NULL,
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND claim_generation = %s
                AND status = 'PROCESSING' AND lease_expires_at > clock_timestamp()""",
                (request.job_id, request.claim_generation),
            ).rowcount
            if changed != 1:
                return PublishResult.LOST_OWNERSHIP
            metadata_json = request.metadata.model_dump_json()
            _ = connection.execute(
                """INSERT INTO rag_enrichment_versions (job_id, generation, metadata)
                VALUES (%s,%s,%s::jsonb)""",
                (request.job_id, request.index_generation, metadata_json),
            )
            _ = connection.execute(
                """INSERT INTO rag_enrichment_publications (
                    job_id, claim_generation, index_generation, article_id,
                    content_hash, enrichment_version, model_version, prompt_version,
                    metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    request.job_id,
                    request.claim_generation,
                    request.index_generation,
                    request.metadata.article_id,
                    request.metadata.content_hash,
                    request.metadata.enrichment_version,
                    request.metadata.model_version,
                    request.metadata.prompt_version,
                    metadata_json,
                ),
            )
            # A newer identity replaces older READY metadata for this article; the
            # rows stay (history), only their eligibility changes.
            _ = connection.execute(
                """UPDATE rag_enrichment_jobs SET status = 'STALE',
                    updated_at = clock_timestamp()
                WHERE article_id = %s AND job_id <> %s AND status = 'READY'""",
                (request.metadata.article_id, request.job_id),
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
            return PublishResult.PUBLISHED
