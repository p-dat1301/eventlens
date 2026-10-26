"""PostgreSQL enrichment job orchestration built on T3 storage primitives.

Every ownership write is a single conditional statement that matches job_id,
claim_generation, status PROCESSING, an unexpired lease on the database clock,
no recorded cancellation for this claim, and a current non-tombstoned article
revision. Zero affected rows means ownership was lost. No transaction is held
open across a provider call: each method opens and commits its own short one.
"""

from dataclasses import dataclass
from typing import Final, LiteralString
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow, scalar_row
from pydantic import TypeAdapter

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.enrichment_lifecycle import (
    CancelAck,
    ClaimedJob,
    EnrichmentIdentity,
    FailureCode,
    IngestReceipt,
    PublishResult,
    RetryPolicy,
)
from backend.rag.domain.records import EnrichmentJobKey
from backend.rag.infra.postgres.connection import connect
from backend.rag.infra.postgres.publication import (
    LockedHook,
    PostgresPublication,
    Publication,
)
from backend.rag.infra.postgres.store import persist_revision
from backend.shared.contracts import ArticleEnrichment

_OWNED: Final = """job_id = %(job_id)s AND claim_generation = %(generation)s
    AND status = 'PROCESSING' AND lease_expires_at > clock_timestamp()
    AND cancel_requested_generation IS DISTINCT FROM claim_generation"""
_CURRENT: Final = """EXISTS (SELECT 1 FROM rag_article_current current
    WHERE current.article_id = rag_enrichment_jobs.article_id
    AND current.content_hash = rag_enrichment_jobs.content_hash
    AND current.tombstoned_at IS NULL)"""
_IDENTITY: Final = """enrichment_version = %(enrichment_version)s
    AND model_version = %(model_version)s AND prompt_version = %(prompt_version)s"""

_ROW = TypeAdapter(tuple[UUID, int, int, UUID, str])
_OPTIONAL_TEXT: TypeAdapter[str | None] = TypeAdapter(str | None)
_OPTIONAL_UUID: TypeAdapter[UUID | None] = TypeAdapter(UUID | None)
_CURRENT_ROW = TypeAdapter(tuple[str, bool])
_JOB_STATUS = TypeAdapter(tuple[UUID, str])


def _identity(identity: EnrichmentIdentity) -> dict[str, str]:
    return {
        "enrichment_version": identity.enrichment_version,
        "model_version": identity.model_version,
        "prompt_version": identity.prompt_version,
    }


def _owned(claim: ClaimedJob) -> dict[str, object]:
    return {"job_id": claim.job_id, "generation": claim.claim_generation}


def _lock_current(connection: Connection[TupleRow], article_id: UUID) -> str | None:
    """Lock the article pointer row; return its hash only while visible.

    Every enqueue path (ingest, reconciliation, T3 ``enqueue_job``) and every
    correction (``persist_revision`` upsert) serializes on this row lock, so the
    decision below never relies on a snapshot read before the lock.
    """
    row = connection.execute(
        """SELECT content_hash, tombstoned_at IS NULL FROM rag_article_current
        WHERE article_id = %s FOR UPDATE""",
        (article_id,),
    ).fetchone()
    if row is None:
        return None
    content_hash, visible = _CURRENT_ROW.validate_python(row)
    return content_hash if visible else None


def _enqueue_locked(
    connection: Connection[TupleRow], key: EnrichmentJobKey, *, requeue_stale: bool
) -> tuple[UUID, bool]:
    """Insert, requeue or no-op under the article lock; caller verified currency.

    Budget fields are initialized only by the transition this transaction
    performs (fresh insert, or STALE -> PENDING requeue for explicit delivery).
    Duplicate delivery of a live job never touches attempts, failure code,
    schedule or fence. Supersession runs only after such a transition, and only
    for the verified current revision.
    """
    params = (
        key.article_id,
        key.content_hash,
        key.enrichment_version,
        key.model_version,
        key.prompt_version,
    )
    inserted = _OPTIONAL_UUID.validate_python(
        connection.cursor(row_factory=scalar_row)
        .execute(
            """INSERT INTO rag_enrichment_jobs (
                job_id, article_id, content_hash, enrichment_version, model_version,
                prompt_version
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (article_id, content_hash, enrichment_version,
                model_version, prompt_version) DO NOTHING
            RETURNING job_id""",
            (uuid4(), *params),
        )
        .fetchone()
    )
    if inserted is not None:
        job_id, transitioned, created = inserted, True, True
    else:
        job_id, status = _JOB_STATUS.validate_python(
            connection.execute(
                """SELECT job_id, status FROM rag_enrichment_jobs
                WHERE article_id = %s AND content_hash = %s
                AND enrichment_version = %s AND model_version = %s
                AND prompt_version = %s FOR UPDATE""",
                params,
            ).fetchone()
        )
        transitioned, created = requeue_stale and status == "STALE", False
        if transitioned:
            _ = connection.execute(
                """UPDATE rag_enrichment_jobs SET status = 'PENDING',
                    attempt_count = 0, failure_code = NULL,
                    next_attempt_at = clock_timestamp(), lease_expires_at = NULL,
                    cancel_requested_generation = NULL,
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND status = 'STALE'""",
                (job_id,),
            )
            # T3 requeue semantics: drop only the per-job current-metadata row;
            # the append-only rag_enrichment_publications history is kept.
            _ = connection.execute(
                "DELETE FROM rag_enrichment_versions WHERE job_id = %s", (job_id,)
            )
    if transitioned:
        _ = connection.execute(
            """UPDATE rag_enrichment_jobs SET status = 'STALE',
                lease_expires_at = NULL, updated_at = clock_timestamp()
            WHERE article_id = %s AND job_id <> %s
            AND status IN ('PENDING', 'PROCESSING')""",
            (key.article_id, job_id),
        )
    return job_id, created


@dataclass(frozen=True, slots=True)
class PostgresEnrichmentRepository:
    """EnrichmentRepository over the RAG schema."""

    database_url: str
    publish_hook: LockedHook | None = None

    def ingest(self, document: ArticleDocument, key: EnrichmentJobKey) -> IngestReceipt:
        """Persist revision and enqueue its job in one transaction (no outbox gap)."""
        with connect(self.database_url) as connection:
            persist_revision(connection, document)
            if _lock_current(connection, document.article_id) != key.content_hash:
                return IngestReceipt.not_current(
                    document.article_id, document.content_hash
                )
            job_id, created = _enqueue_locked(connection, key, requeue_stale=True)
        return IngestReceipt.enqueued(
            document.article_id, document.content_hash, job_id, created=created
        )

    def enqueue_missing(self, identity: EnrichmentIdentity, limit: int) -> int:
        """Reconciliation sweep: current revisions without a job for identity."""
        with connect(self.database_url) as connection:
            rows = TypeAdapter(list[tuple[UUID, str]]).validate_python(
                connection.execute(
                    f"""SELECT current.article_id, current.content_hash
                    FROM rag_article_current current
                    WHERE current.tombstoned_at IS NULL AND NOT EXISTS (
                        SELECT 1 FROM rag_enrichment_jobs
                        WHERE rag_enrichment_jobs.article_id = current.article_id
                        AND rag_enrichment_jobs.content_hash = current.content_hash
                        AND {_IDENTITY})
                    ORDER BY current.article_id LIMIT %(limit)s""",  # noqa: S608
                    {**_identity(identity), "limit": limit},
                ).fetchall()
            )
            created = 0
            for article_id, _candidate_hash in rows:
                # The candidate snapshot may be obsolete: re-read the revision
                # under the article lock and enqueue only what is current now.
                current_hash = _lock_current(connection, article_id)
                if current_hash is None:
                    continue
                _, fresh = _enqueue_locked(
                    connection,
                    identity.key_for(article_id, current_hash),
                    requeue_stale=False,
                )
                created += fresh
            return created

    def claim_next(
        self, identity: EnrichmentIdentity, policy: RetryPolicy
    ) -> ClaimedJob | None:
        """Sweep, then claim one eligible job with FOR UPDATE SKIP LOCKED."""
        params = {
            **_identity(identity),
            "max_attempts": policy.max_attempts,
            "lease": policy.lease_seconds,
            "policy_id": policy.policy_id,
        }
        with connect(self.database_url) as connection:
            _ = connection.execute(
                f"""UPDATE rag_enrichment_jobs SET status = 'STALE',
                    lease_expires_at = NULL, updated_at = clock_timestamp()
                WHERE status IN ('PENDING', 'PROCESSING') AND {_IDENTITY}
                AND NOT {_CURRENT}""",  # noqa: S608
                params,
            )
            _ = connection.execute(
                f"""UPDATE rag_enrichment_jobs SET status = 'FAILED',
                    failure_code = 'lease_expired', lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE status = 'PROCESSING' AND lease_expires_at <= clock_timestamp()
                AND attempt_count >= %(max_attempts)s AND {_IDENTITY}""",  # noqa: S608
                params,
            )
        with connect(self.database_url) as connection:
            row = connection.execute(
                f"""WITH candidate AS (
                    SELECT job_id FROM rag_enrichment_jobs
                    WHERE {_IDENTITY} AND attempt_count < %(max_attempts)s
                    AND ((status = 'PENDING' AND next_attempt_at <= clock_timestamp())
                      OR (status = 'PROCESSING'
                          AND lease_expires_at <= clock_timestamp()))
                    AND {_CURRENT}
                    ORDER BY next_attempt_at, created_at, job_id
                    LIMIT 1 FOR UPDATE SKIP LOCKED)
                UPDATE rag_enrichment_jobs job SET status = 'PROCESSING',
                    claim_generation = job.claim_generation + 1,
                    attempt_count = job.attempt_count + 1,
                    lease_expires_at = clock_timestamp()
                        + make_interval(secs => %(lease)s),
                    retry_policy_id = %(policy_id)s, updated_at = clock_timestamp()
                FROM candidate WHERE job.job_id = candidate.job_id
                RETURNING job.job_id, job.claim_generation, job.attempt_count,
                    job.article_id, job.content_hash""",  # noqa: S608
                params,
            ).fetchone()
        if row is None:
            return None
        job_id, generation, attempt, article_id, content_hash = _ROW.validate_python(
            row
        )
        return ClaimedJob(
            job_id=job_id,
            claim_generation=generation,
            attempt=attempt,
            key=identity.key_for(article_id, content_hash),
        )

    def load_current(self, claim: ClaimedJob) -> ArticleDocument | None:
        """Read the owned, current, visible revision (short read transaction)."""
        with connect(self.database_url) as connection:
            row = _OPTIONAL_TEXT.validate_python(
                connection.cursor(row_factory=scalar_row)
                .execute(
                    f"""SELECT document.document_json::text
                    FROM rag_enrichment_jobs
                    JOIN rag_article_documents document
                        USING (article_id, content_hash)
                    WHERE {_OWNED} AND {_CURRENT}""",  # noqa: S608
                    _owned(claim),
                )
                .fetchone()
            )
        return None if row is None else ArticleDocument.model_validate_json(row)

    def _write(self, statement: LiteralString, params: dict[str, object]) -> bool:
        with connect(self.database_url) as connection:
            return connection.execute(statement, params).rowcount == 1

    def schedule_retry(
        self, claim: ClaimedJob, code: FailureCode, delay_seconds: int
    ) -> bool:
        """Fenced PROCESSING -> PENDING; eligibility uses the DB clock."""
        return self._write(
            f"""UPDATE rag_enrichment_jobs SET status = 'PENDING',
                failure_code = %(code)s, lease_expires_at = NULL,
                next_attempt_at = clock_timestamp() + make_interval(secs => %(delay)s),
                updated_at = clock_timestamp()
            WHERE {_OWNED} AND {_CURRENT}""",  # noqa: S608
            {**_owned(claim), "code": code.value, "delay": delay_seconds},
        )

    def mark_failed(self, claim: ClaimedJob, code: FailureCode) -> bool:
        """Fenced PROCESSING -> FAILED; article and lexical row stay intact."""
        return self._write(
            f"""UPDATE rag_enrichment_jobs SET status = 'FAILED',
                failure_code = %(code)s, lease_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE {_OWNED} AND {_CURRENT}""",  # noqa: S608
            {**_owned(claim), "code": code.value},
        )

    def mark_stale(self, claim: ClaimedJob) -> bool:
        """Fenced PROCESSING -> STALE only while the revision is not current."""
        return self._write(
            f"""UPDATE rag_enrichment_jobs SET status = 'STALE',
                lease_expires_at = NULL, updated_at = clock_timestamp()
            WHERE {_OWNED} AND NOT {_CURRENT}""",  # noqa: S608
            _owned(claim),
        )

    def request_cancel(self, claim: ClaimedJob) -> CancelAck:
        """Record cancellation under the job row lock publication also takes."""
        with connect(self.database_url) as connection:
            acknowledged = connection.execute(
                """UPDATE rag_enrichment_jobs
                SET cancel_requested_generation = claim_generation,
                    updated_at = clock_timestamp()
                WHERE job_id = %s AND claim_generation = %s
                AND status = 'PROCESSING'""",
                (claim.job_id, claim.claim_generation),
            ).rowcount
            if acknowledged == 1:
                return CancelAck.ACKNOWLEDGED
            row = connection.execute(
                """SELECT status, claim_generation FROM rag_enrichment_jobs
                WHERE job_id = %s""",
                (claim.job_id,),
            ).fetchone()
        if row == ("READY", claim.claim_generation):
            return CancelAck.ALREADY_PUBLISHED
        return CancelAck.OWNERSHIP_LOST

    def publish(
        self, claim: ClaimedJob, index_generation: int, metadata: ArticleEnrichment
    ) -> PublishResult:
        """Delegate to the T3 fenced publication primitive."""
        return PostgresPublication(self.database_url).publish_fenced(
            Publication(
                job_id=claim.job_id,
                claim_generation=claim.claim_generation,
                index_generation=index_generation,
                metadata=metadata,
            ),
            after_lock=self.publish_hook,
        )
