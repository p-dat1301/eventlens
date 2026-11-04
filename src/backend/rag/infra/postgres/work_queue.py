"""PostgreSQL ``WorkQueue`` over ``rag_work_jobs`` (revision ``rag_0004``)."""

# ruff: noqa: S608 - SQL is built from reviewed constant fragments; values are bound.

from datetime import datetime
from typing import LiteralString
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import Connection, Engine

from backend.rag.domain.work_jobs import (
    ClaimedWorkJob,
    ExpiredWorkJob,
    JobFence,
    JobKind,
)
from backend.rag.infra.postgres.engine import execute, first, rows, transaction

FENCE: LiteralString = """job_id = %(job)s::uuid AND claim_generation = %(gen)s
    AND status = 'PROCESSING' AND lease_expires_at > clock_timestamp()"""

EXPIRE_SQL: LiteralString = """
UPDATE rag_work_jobs SET status = 'FAILED', lease_expires_at = NULL,
    failure_code = CASE
        WHEN deadline_at IS NOT NULL AND deadline_at <= clock_timestamp()
        THEN 'deadline_exceeded' ELSE 'lease_expired' END,
    updated_at = clock_timestamp()
WHERE kind = %(kind)s AND (
    (status = 'PROCESSING' AND lease_expires_at <= clock_timestamp()
        AND attempt_count >= max_attempts)
    OR (deadline_at IS NOT NULL AND deadline_at <= clock_timestamp() AND (
        status = 'PENDING' OR (status = 'PROCESSING'
                               AND lease_expires_at <= clock_timestamp()))))
RETURNING job_id, subject_id, failure_code
"""

CLAIM_SQL: LiteralString = """
UPDATE rag_work_jobs job SET status = 'PROCESSING',
    attempt_count = job.attempt_count + 1,
    claim_generation = job.claim_generation + 1,
    lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s),
    worker_id = %(worker)s, updated_at = clock_timestamp()
FROM (
    SELECT job_id FROM rag_work_jobs
    WHERE kind = %(kind)s AND attempt_count < max_attempts
      AND (deadline_at IS NULL OR deadline_at > clock_timestamp())
      AND ((status = 'PENDING' AND next_attempt_at <= clock_timestamp())
           OR (status = 'PROCESSING' AND lease_expires_at <= clock_timestamp()))
    ORDER BY priority DESC, next_attempt_at, created_at, job_id
    FOR UPDATE SKIP LOCKED
    LIMIT %(limit)s
) due
WHERE job.job_id = due.job_id
RETURNING job.job_id, job.subject_id, job.revision_key, job.claim_generation,
          job.attempt_count, job.max_attempts, job.deadline_at
"""

EXTEND_SQL: LiteralString = (
    """UPDATE rag_work_jobs
SET lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s),
    updated_at = clock_timestamp()
WHERE """
    + FENCE
    + " RETURNING 1"
)

_EXPIRED = TypeAdapter(list[tuple[UUID, UUID, str]])
_CLAIMED = TypeAdapter(list[tuple[UUID, UUID, str, int, int, int, datetime | None]])


def fence_params(fence: JobFence) -> dict[str, object]:
    """Bind the claim fence."""
    return {"job": fence.job_id, "gen": fence.claim_generation}


def fenced_update(
    connection: Connection,
    fence: JobFence,
    assignments: LiteralString,
    **values: object,
) -> str | None:
    """Apply ``assignments`` under the fence; the new status or ``None``."""
    sql = (
        "UPDATE rag_work_jobs SET "
        + assignments
        + ", lease_expires_at = NULL, updated_at = clock_timestamp() WHERE "
        + FENCE
        + " RETURNING status"
    )
    row = first(execute(connection, sql, fence_params(fence) | values))
    return None if row is None else str(row[0])


class PostgresWorkQueue:
    """``WorkQueue``: one short transaction per call."""

    def __init__(self, engine: Engine) -> None:
        """Bind the shared engine."""
        self._engine: Engine = engine

    def expire(self, kind: JobKind) -> tuple[ExpiredWorkJob, ...]:
        """Fail exhausted leases and passed deadlines."""
        with transaction(self._engine) as connection:
            found = _EXPIRED.validate_python(
                rows(execute(connection, EXPIRE_SQL, {"kind": kind.value}))
            )
        return tuple(ExpiredWorkJob(*row) for row in found)

    def claim(
        self, kind: JobKind, worker_id: str, lease_seconds: float, limit: int
    ) -> tuple[ClaimedWorkJob, ...]:
        """Claim due jobs; an expired lease is re-claimed as a new attempt."""
        with transaction(self._engine) as connection:
            found = _CLAIMED.validate_python(
                rows(
                    execute(
                        connection,
                        CLAIM_SQL,
                        {
                            "kind": kind.value,
                            "worker": worker_id,
                            "lease": lease_seconds,
                            "limit": limit,
                        },
                    )
                )
            )
        return tuple(
            ClaimedWorkJob(
                fence=JobFence(job_id, generation),
                kind=kind,
                subject_id=subject,
                revision_key=revision,
                attempt=attempt,
                max_attempts=max_attempts,
                deadline_at=deadline,
            )
            for job_id, subject, revision, generation, attempt, max_attempts, deadline in found  # noqa: E501
        )

    def extend(self, fence: JobFence, lease_seconds: float) -> bool:
        """Push the lease out while the fence holds."""
        with transaction(self._engine) as connection:
            row = first(
                execute(
                    connection,
                    EXTEND_SQL,
                    fence_params(fence) | {"lease": lease_seconds},
                )
            )
        return row is not None

    def complete(self, fence: JobFence) -> bool:
        """DONE under the fence."""
        with transaction(self._engine) as connection:
            return fenced_update(connection, fence, "status = 'DONE'") is not None

    def retry(self, fence: JobFence, code: str, delay_seconds: float) -> str | None:
        """PENDING with backoff, or FAILED when the attempt budget is spent."""
        with transaction(self._engine) as connection:
            return fenced_update(
                connection,
                fence,
                """status = CASE WHEN attempt_count < max_attempts THEN 'PENDING'
                    ELSE 'FAILED' END,
                failure_code = %(code)s,
                next_attempt_at = clock_timestamp()
                    + make_interval(secs => %(delay)s)""",
                code=code,
                delay=delay_seconds,
            )

    def fail(self, fence: JobFence, code: str) -> bool:
        """Terminal FAILED."""
        with transaction(self._engine) as connection:
            return (
                fenced_update(
                    connection,
                    fence,
                    "status = 'FAILED', failure_code = %(code)s",
                    code=code,
                )
                is not None
            )

    def mark(self, fence: JobFence, status: str, code: str | None) -> bool:
        """Terminal STALE or CANCELLED."""
        if status not in {"STALE", "CANCELLED"}:
            msg = "mark accepts STALE or CANCELLED only"
            raise ValueError(msg)
        with transaction(self._engine) as connection:
            return (
                fenced_update(
                    connection,
                    fence,
                    "status = %(status)s, failure_code = %(code)s",
                    status=status,
                    code=code,
                )
                is not None
            )


REQUEUE_SUMMARY_SQL: LiteralString = """
WITH current AS (
    SELECT article_id, content_hash FROM rag_article_current
    WHERE article_id = %(id)s::uuid AND tombstoned_at IS NULL
), reset AS (
    UPDATE rag_work_jobs job SET status = 'PENDING', attempt_count = 0,
        failure_code = NULL, lease_expires_at = NULL,
        next_attempt_at = clock_timestamp(), updated_at = clock_timestamp()
    FROM current
    WHERE job.kind = 'summary' AND job.subject_id = current.article_id
      AND job.revision_key = current.content_hash
      AND job.status IN ('FAILED', 'DONE', 'STALE', 'CANCELLED')
    RETURNING job.job_id
), inserted AS (
    INSERT INTO rag_work_jobs (job_id, kind, subject_id, revision_key, max_attempts)
    SELECT %(job)s::uuid, 'summary', article_id, content_hash, 3 FROM current
    ON CONFLICT (kind, subject_id, revision_key) DO NOTHING
    RETURNING job_id
)
SELECT (SELECT count(*) FROM current), (SELECT count(*) FROM reset),
       (SELECT count(*) FROM inserted)
"""


def requeue_summary(engine: Engine, article_id: UUID) -> str:
    """Admin retry of the current revision's summary job.

    ``requeued`` (terminal job reset, attempts zeroed), ``scheduled`` (no job
    existed), ``already_pending`` or ``not_current`` (unknown/tombstoned).
    A summary already published for the same model/prompt/schema identity is
    kept (first publication wins); regenerate by changing that identity.
    """
    with transaction(engine) as connection:
        row = first(
            execute(connection, REQUEUE_SUMMARY_SQL, {"id": article_id, "job": uuid4()})
        )
    current, reset, inserted = TypeAdapter(tuple[int, int, int]).validate_python(row)
    if not current:
        return "not_current"
    if reset:
        return "requeued"
    return "scheduled" if inserted else "already_pending"
