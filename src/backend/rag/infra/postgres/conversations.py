"""PostgreSQL sessions, conversations, runs and citations (``rag_0004``).

Owner-facing statements always match ``owner_id``. ``submit`` locks the
conversation row, so concurrent submits serialize: an equal Idempotency-Key
replays, a different body with the same key is a conflict, and a second
active run is refused (also enforced by a partial unique index). Run
completion, the assistant message, citations and the job's DONE state are
one transaction under the job's claim fence.
"""

# ruff: noqa: S608 - SQL is built from reviewed constant fragments; values are bound.

import json
from collections.abc import Callable
from datetime import datetime
from typing import LiteralString
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine

from backend.rag.application.ports.conversations import NewRun, SubmitResult
from backend.rag.domain.conversation import (
    Conversation,
    ConversationNotFoundError,
    ConversationStorageError,
    IdempotencyConflictError,
    Message,
    QueryRun,
    RunCompletion,
    RunInProgressError,
    RunOutcome,
    RunStatus,
    RunWork,
    SessionPrincipal,
    StoredCitation,
)
from backend.rag.domain.work_jobs import JobFence
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import execute, first, rows, transaction
from backend.rag.infra.postgres.work_queue import (
    EXPIRE_SQL,
    FENCE,
    fence_params,
    fenced_update,
)

CONVERSATION_COLUMNS: LiteralString = """conversation_id, owner_id, title, scope,
    scope_sha256, created_at, updated_at"""

RUN_COLUMNS: LiteralString = """run_id, conversation_id, owner_id, sequence,
    question, status, outcome, abstention_reason, failure_code, failure_reason,
    answer, effective_scope, as_of, source_versions, usage,
    cancel_requested_at IS NOT NULL, created_at, started_at, finished_at"""

# Non-terminal runs whose job can no longer run them (expired, failed,
# cancelled, missing): fail or cancel the run. Runs after the expiry update
# in the same transaction, and on every claim, so a crash between the two
# writes (or before this ever ran) is repaired on the next claim/restart.
RECONCILE_RUNS_SQL: LiteralString = """
UPDATE rag_query_runs r SET
    status = CASE WHEN o.job_status = 'CANCELLED' THEN 'cancelled' ELSE 'failed' END,
    failure_code = CASE WHEN o.job_status = 'CANCELLED' THEN NULL
                   ELSE coalesce(o.job_failure, 'orphaned_run') END,
    finished_at = clock_timestamp(),
    cancel_requested_at = CASE WHEN o.job_status = 'CANCELLED'
        THEN coalesce(r.cancel_requested_at, clock_timestamp())
        ELSE r.cancel_requested_at END
FROM (
    SELECT run.run_id, job.status AS job_status, job.failure_code AS job_failure
    FROM rag_query_runs run
    LEFT JOIN rag_work_jobs job
        ON job.kind = 'query_run' AND job.subject_id = run.run_id
    WHERE run.status IN ('queued', 'running')
      AND (job.job_id IS NULL OR job.status NOT IN ('PENDING', 'PROCESSING'))
    FOR UPDATE OF run SKIP LOCKED
) o
WHERE r.run_id = o.run_id
RETURNING r.run_id
"""

CITATION_COLUMNS: LiteralString = """citation_id, ordinal, claim_id, article_id,
    content_hash, span_id, start_offset, end_offset, excerpt, span_fingerprint,
    source_title, source_name, source_domain, source_url, source_published_at"""

_CONVERSATION = TypeAdapter(
    tuple[UUID, UUID, str | None, dict[str, object], str, datetime, datetime]
)
_RUN = TypeAdapter(
    tuple[
        UUID,
        UUID,
        UUID,
        int,
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        dict[str, object] | None,
        dict[str, object] | None,
        datetime | None,
        dict[str, str] | None,
        dict[str, object] | None,
        bool,
        datetime,
        datetime | None,
        datetime | None,
    ]
)
_CITATION = TypeAdapter(
    tuple[
        UUID,
        int,
        UUID,
        UUID,
        str,
        UUID,
        int,
        int,
        str,
        str,
        str,
        str,
        str,
        str,
        datetime | None,
    ]
)
_MESSAGE = TypeAdapter(tuple[UUID, UUID, str, int, str, datetime])
_SESSION = TypeAdapter(tuple[UUID, datetime])
_EXPIRY = TypeAdapter(datetime)


def _conversation(row: object) -> Conversation:
    return Conversation(*_CONVERSATION.validate_python(row))


def _citation(row: object) -> StoredCitation:
    return StoredCitation(*_CITATION.validate_python(row))


def _run(row: object, citations: tuple[StoredCitation, ...]) -> QueryRun:
    (
        run_id,
        conversation_id,
        owner_id,
        sequence,
        question,
        status,
        outcome,
        abstention,
        failure,
        failure_reason,
        answer,
        scope,
        as_of,
        versions,
        usage,
        cancel_requested,
        created,
        started,
        finished,
    ) = _RUN.validate_python(row)
    return QueryRun(
        run_id=run_id,
        conversation_id=conversation_id,
        owner_id=owner_id,
        sequence=sequence,
        question=question,
        status=RunStatus(status),
        outcome=None if outcome is None else RunOutcome(outcome),
        abstention_reason=abstention,
        failure_code=failure,
        failure_reason=failure_reason,
        answer=answer,
        effective_scope=scope,
        as_of=as_of,
        source_versions=versions,
        usage=usage,
        cancel_requested=cancel_requested,
        created_at=created,
        started_at=started,
        finished_at=finished,
        citations=citations,
    )


class _Storage:
    def __init__(self, engine: Engine) -> None:
        self._engine: Engine = engine

    def _tx[T](self, work: Callable[[Connection], T], *, read_only: bool = False) -> T:
        try:
            with transaction(self._engine, read_only=read_only) as connection:
                return work(connection)
        except (StorageUnavailableError, StorageOperationError, ValidationError):
            raise ConversationStorageError from None


class PostgresSessions(_Storage):
    """``SessionStore``: only the token's SHA-256 is stored."""

    def create(self, token_sha256: str, owner_id: UUID, ttl_seconds: int) -> datetime:
        """Insert one session."""

        def work(connection: Connection) -> datetime:
            row = first(
                execute(
                    connection,
                    """INSERT INTO rag_sessions (token_sha256, owner_id, expires_at)
                    VALUES (%(token)s, %(owner)s::uuid,
                            clock_timestamp() + make_interval(secs => %(ttl)s))
                    RETURNING expires_at""",
                    {"token": token_sha256, "owner": owner_id, "ttl": ttl_seconds},
                )
            )
            return _EXPIRY.validate_python(None if row is None else row[0])

        return self._tx(work)

    def resolve(self, token_sha256: str) -> SessionPrincipal | None:
        """Unexpired, unrevoked session."""

        def work(connection: Connection) -> SessionPrincipal | None:
            row = first(
                execute(
                    connection,
                    """SELECT owner_id, expires_at FROM rag_sessions
                    WHERE token_sha256 = %(token)s AND revoked_at IS NULL
                      AND expires_at > clock_timestamp()""",
                    {"token": token_sha256},
                )
            )
            if row is None:
                return None
            return SessionPrincipal(*_SESSION.validate_python(row))

        return self._tx(work, read_only=True)

    def revoke(self, token_sha256: str) -> None:
        """Revoke idempotently."""

        def work(connection: Connection) -> None:
            _ = execute(
                connection,
                """UPDATE rag_sessions SET revoked_at = coalesce(revoked_at,
                    clock_timestamp()) WHERE token_sha256 = %(token)s""",
                {"token": token_sha256},
            )

        self._tx(work)


GET_RUN_SQL: LiteralString = (
    "SELECT "
    + RUN_COLUMNS
    + " FROM rag_query_runs WHERE run_id = %(run)s::uuid AND owner_id = %(owner)s::uuid"
)
CITATIONS_SQL: LiteralString = (
    "SELECT "
    + CITATION_COLUMNS
    + " FROM rag_run_citations WHERE run_id = %(run)s::uuid ORDER BY ordinal"
)


class PostgresConversations(_Storage):
    """``ConversationStore`` over the RAG schema."""

    def create(
        self,
        owner_id: UUID,
        conversation_id: UUID,
        title: str | None,
        scope: dict[str, object],
        scope_sha256: str,
    ) -> Conversation:
        """Insert one conversation."""

        def work(connection: Connection) -> Conversation:
            row = first(
                execute(
                    connection,
                    """INSERT INTO rag_conversations (conversation_id, owner_id,
                        title, scope, scope_sha256)
                    VALUES (%(id)s::uuid, %(owner)s::uuid, %(title)s,
                        %(scope)s::jsonb, %(sha)s)
                    RETURNING """
                    + CONVERSATION_COLUMNS,
                    {
                        "id": conversation_id,
                        "owner": owner_id,
                        "title": title,
                        "scope": json.dumps(scope),
                        "sha": scope_sha256,
                    },
                )
            )
            return _conversation(row)

        return self._tx(work)

    def list(
        self, owner_id: UUID, limit: int, before: tuple[datetime, UUID] | None
    ) -> tuple[Conversation, ...]:
        """Newest first; ``before`` is the last (created_at, id) seen."""

        def work(connection: Connection) -> tuple[Conversation, ...]:
            found = rows(
                execute(
                    connection,
                    "SELECT "
                    + CONVERSATION_COLUMNS
                    + """ FROM rag_conversations
                    WHERE owner_id = %(owner)s::uuid AND deleted_at IS NULL
                      AND (NOT %(keyset)s OR (created_at, conversation_id)
                           < (%(at)s::timestamptz, %(id)s::uuid))
                    ORDER BY created_at DESC, conversation_id DESC
                    LIMIT %(limit)s""",
                    {
                        "owner": owner_id,
                        "keyset": before is not None,
                        "at": None if before is None else before[0],
                        "id": None if before is None else before[1],
                        "limit": limit,
                    },
                )
            )
            return tuple(_conversation(row) for row in found)

        return self._tx(work, read_only=True)

    def get(self, owner_id: UUID, conversation_id: UUID) -> Conversation | None:
        """Owned, not deleted."""

        def work(connection: Connection) -> Conversation | None:
            row = first(
                execute(
                    connection,
                    "SELECT "
                    + CONVERSATION_COLUMNS
                    + """ FROM rag_conversations WHERE conversation_id = %(id)s::uuid
                    AND owner_id = %(owner)s::uuid AND deleted_at IS NULL""",
                    {"id": conversation_id, "owner": owner_id},
                )
            )
            return None if row is None else _conversation(row)

        return self._tx(work, read_only=True)

    def delete(self, owner_id: UUID, conversation_id: UUID) -> bool:
        """Soft delete; queued runs cancelled now, running runs flagged."""

        def work(connection: Connection) -> bool:
            row = first(
                execute(
                    connection,
                    """UPDATE rag_conversations SET deleted_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE conversation_id = %(id)s::uuid
                      AND owner_id = %(owner)s::uuid AND deleted_at IS NULL
                    RETURNING 1""",
                    {"id": conversation_id, "owner": owner_id},
                )
            )
            if row is None:
                return False
            _ = execute(
                connection,
                """UPDATE rag_work_jobs SET status = 'CANCELLED',
                    lease_expires_at = NULL, failure_code = 'conversation_deleted',
                    updated_at = clock_timestamp()
                WHERE kind = 'query_run' AND status IN ('PENDING', 'PROCESSING')
                  AND subject_id IN (SELECT run_id FROM rag_query_runs
                      WHERE conversation_id = %(id)s::uuid AND status = 'queued')""",
                {"id": conversation_id},
            )
            _ = execute(
                connection,
                """UPDATE rag_query_runs SET
                    cancel_requested_at = coalesce(cancel_requested_at,
                                                   clock_timestamp()),
                    status = CASE WHEN status = 'queued' THEN 'cancelled'
                             ELSE status END,
                    finished_at = CASE WHEN status = 'queued' THEN clock_timestamp()
                                  ELSE finished_at END
                WHERE conversation_id = %(id)s::uuid
                  AND status IN ('queued', 'running')""",
                {"id": conversation_id},
            )
            return True

        return self._tx(work)

    def messages(
        self, owner_id: UUID, conversation_id: UUID, after_sequence: int, limit: int
    ) -> tuple[Message, ...]:
        """Messages of an owned conversation."""

        def work(connection: Connection) -> tuple[Message, ...]:
            found = rows(
                execute(
                    connection,
                    """SELECT m.message_id, m.run_id, m.role, m.sequence, m.content,
                        m.created_at
                    FROM rag_messages m JOIN rag_conversations c
                        ON c.conversation_id = m.conversation_id
                    WHERE m.conversation_id = %(id)s::uuid
                      AND c.owner_id = %(owner)s::uuid AND c.deleted_at IS NULL
                      AND m.sequence > %(after)s
                    ORDER BY m.sequence LIMIT %(limit)s""",
                    {
                        "id": conversation_id,
                        "owner": owner_id,
                        "after": after_sequence,
                        "limit": limit,
                    },
                )
            )
            return tuple(Message(*_MESSAGE.validate_python(row)) for row in found)

        return self._tx(work, read_only=True)

    @staticmethod
    def _load_run(connection: Connection, owner_id: UUID, run_id: UUID) -> QueryRun:
        row = first(
            execute(connection, GET_RUN_SQL, {"run": run_id, "owner": owner_id})
        )
        citations = tuple(
            _citation(item)
            for item in rows(execute(connection, CITATIONS_SQL, {"run": run_id}))
        )
        return _run(row, citations)

    def submit(
        self, owner_id: UUID, conversation_id: UUID, new: NewRun
    ) -> SubmitResult:
        """Idempotent submit under the conversation row lock."""

        def work(connection: Connection) -> SubmitResult:
            locked = first(
                execute(
                    connection,
                    """SELECT next_sequence FROM rag_conversations
                    WHERE conversation_id = %(id)s::uuid
                      AND owner_id = %(owner)s::uuid AND deleted_at IS NULL
                    FOR UPDATE""",
                    {"id": conversation_id, "owner": owner_id},
                )
            )
            if locked is None:
                raise ConversationNotFoundError
            sequence = TypeAdapter(int).validate_python(locked[0])
            if new.idempotency_key is not None:
                existing = first(
                    execute(
                        connection,
                        """SELECT run_id, request_sha256 FROM rag_query_runs
                        WHERE owner_id = %(owner)s::uuid
                          AND conversation_id = %(id)s::uuid
                          AND idempotency_key = %(key)s""",
                        {
                            "owner": owner_id,
                            "id": conversation_id,
                            "key": new.idempotency_key,
                        },
                    )
                )
                if existing is not None:
                    run_id, sha = TypeAdapter(tuple[UUID, str]).validate_python(
                        existing
                    )
                    if sha != new.request_sha256:
                        raise IdempotencyConflictError
                    return SubmitResult(
                        self._load_run(connection, owner_id, run_id), created=False
                    )
            active = first(
                execute(
                    connection,
                    """SELECT 1 FROM rag_query_runs WHERE conversation_id =
                    %(id)s::uuid AND status IN ('queued', 'running')""",
                    {"id": conversation_id},
                )
            )
            if active is not None:
                raise RunInProgressError
            params = {
                "run": new.run_id,
                "id": conversation_id,
                "owner": owner_id,
                "seq": sequence,
                "question": new.question,
                "key": new.idempotency_key,
                "sha": new.request_sha256,
                "message": new.user_message_id,
                "job": new.job_id,
                "attempts": new.max_attempts,
                "deadline": new.deadline_seconds,
            }
            for statement in (
                """INSERT INTO rag_query_runs (run_id, conversation_id, owner_id,
                    sequence, question, idempotency_key, request_sha256)
                VALUES (%(run)s::uuid, %(id)s::uuid, %(owner)s::uuid, %(seq)s,
                    %(question)s, %(key)s, %(sha)s)""",
                """INSERT INTO rag_messages (message_id, conversation_id, run_id,
                    role, sequence, content)
                VALUES (%(message)s::uuid, %(id)s::uuid, %(run)s::uuid, 'user',
                    %(seq)s, %(question)s)""",
                """INSERT INTO rag_work_jobs (job_id, kind, subject_id,
                    revision_key, priority, max_attempts, deadline_at)
                VALUES (%(job)s::uuid, 'query_run', %(run)s::uuid, 'run', 10,
                    %(attempts)s,
                    clock_timestamp() + make_interval(secs => %(deadline)s))""",
                """UPDATE rag_conversations SET next_sequence = %(seq)s + 2,
                    updated_at = clock_timestamp()
                WHERE conversation_id = %(id)s::uuid""",
            ):
                _ = execute(connection, statement, params)
            return SubmitResult(
                self._load_run(connection, owner_id, new.run_id), created=True
            )

        return self._tx(work)

    def run(self, owner_id: UUID, run_id: UUID) -> QueryRun | None:
        """Owned run with citations."""

        def work(connection: Connection) -> QueryRun | None:
            row = first(
                execute(connection, GET_RUN_SQL, {"run": run_id, "owner": owner_id})
            )
            if row is None:
                return None
            return self._load_run(connection, owner_id, run_id)

        return self._tx(work, read_only=True)

    def citation(
        self, owner_id: UUID, run_id: UUID, citation_id: UUID
    ) -> StoredCitation | None:
        """One citation of an owned run."""

        def work(connection: Connection) -> StoredCitation | None:
            row = first(
                execute(
                    connection,
                    "SELECT "
                    + CITATION_COLUMNS
                    + """ FROM rag_run_citations c WHERE c.citation_id =
                    %(citation)s::uuid AND c.run_id = %(run)s::uuid AND EXISTS (
                        SELECT 1 FROM rag_query_runs r
                        WHERE r.run_id = c.run_id AND r.owner_id = %(owner)s::uuid)""",
                    {"citation": citation_id, "run": run_id, "owner": owner_id},
                )
            )
            return None if row is None else _citation(row)

        return self._tx(work, read_only=True)

    def request_cancel(self, owner_id: UUID, run_id: UUID) -> QueryRun | None:
        """Cancel queued runs now; flag running ones; terminal ones unchanged."""

        def work(connection: Connection) -> QueryRun | None:
            row = first(
                execute(
                    connection,
                    """SELECT status FROM rag_query_runs WHERE run_id = %(run)s::uuid
                    AND owner_id = %(owner)s::uuid FOR UPDATE""",
                    {"run": run_id, "owner": owner_id},
                )
            )
            if row is None:
                return None
            status = RunStatus(str(row[0]))
            if status is RunStatus.QUEUED:
                _ = execute(
                    connection,
                    """UPDATE rag_query_runs SET status = 'cancelled',
                        cancel_requested_at = clock_timestamp(),
                        finished_at = clock_timestamp()
                    WHERE run_id = %(run)s::uuid""",
                    {"run": run_id},
                )
                _ = execute(
                    connection,
                    """UPDATE rag_work_jobs SET status = 'CANCELLED',
                        lease_expires_at = NULL, failure_code = 'cancelled',
                        updated_at = clock_timestamp()
                    WHERE kind = 'query_run' AND subject_id = %(run)s::uuid
                      AND status IN ('PENDING', 'PROCESSING')""",
                    {"run": run_id},
                )
            elif status is RunStatus.RUNNING:
                _ = execute(
                    connection,
                    """UPDATE rag_query_runs SET cancel_requested_at =
                        coalesce(cancel_requested_at, clock_timestamp())
                    WHERE run_id = %(run)s::uuid""",
                    {"run": run_id},
                )
            return self._load_run(connection, owner_id, run_id)

        return self._tx(work)

    def start(self, run_id: UUID) -> RunWork | None:
        """queued/running (re-claim after a crash) -> running, unless cancelled."""

        def work(connection: Connection) -> RunWork | None:
            row = first(
                execute(
                    connection,
                    """UPDATE rag_query_runs r SET status = 'running',
                        started_at = coalesce(r.started_at, clock_timestamp())
                    FROM rag_conversations c
                    WHERE r.run_id = %(run)s::uuid
                      AND c.conversation_id = r.conversation_id
                      AND c.deleted_at IS NULL
                      AND r.status IN ('queued', 'running')
                      AND r.cancel_requested_at IS NULL
                    RETURNING r.conversation_id, r.owner_id, r.question, c.scope,
                        r.sequence""",
                    {"run": run_id},
                )
            )
            if row is None:
                return None
            conversation_id, owner_id, question, scope, sequence = TypeAdapter(
                tuple[UUID, UUID, str, dict[str, object], int]
            ).validate_python(row)
            previous = TypeAdapter(list[tuple[UUID, str]]).validate_python(
                rows(
                    execute(
                        connection,
                        """SELECT message_id, content FROM rag_messages
                        WHERE conversation_id = %(id)s::uuid AND role = 'user'
                          AND sequence < %(seq)s
                        ORDER BY sequence DESC LIMIT 3""",
                        {"id": conversation_id, "seq": sequence},
                    )
                )
            )
            return RunWork(
                run_id=run_id,
                conversation_id=conversation_id,
                owner_id=owner_id,
                question=question,
                scope=scope,
                previous_questions=tuple(text for _, text in reversed(previous)),
                previous_message_ids=tuple(str(m) for m, _ in reversed(previous)),
            )

        return self._tx(work)

    def reconcile_runs(self) -> int:
        """Expire exhausted run jobs and settle orphaned runs atomically."""

        def work(connection: Connection) -> int:
            _ = execute(connection, EXPIRE_SQL, {"kind": "query_run"})
            return len(rows(execute(connection, RECONCILE_RUNS_SQL)))

        return self._tx(work)

    def cancel_requested(self, run_id: UUID) -> bool:
        """Cancel flag of a run (missing run counts as cancelled)."""

        def work(connection: Connection) -> bool:
            row = first(
                execute(
                    connection,
                    """SELECT cancel_requested_at IS NOT NULL OR status <> 'running'
                    FROM rag_query_runs WHERE run_id = %(run)s::uuid""",
                    {"run": run_id},
                )
            )
            return row is None or bool(row[0])

        return self._tx(work, read_only=True)

    @staticmethod
    def _owned(connection: Connection, fence: JobFence) -> bool:
        row = first(
            execute(
                connection,
                "SELECT 1 FROM rag_work_jobs WHERE " + FENCE + " FOR UPDATE",
                fence_params(fence),
            )
        )
        return row is not None

    def complete(self, fence: JobFence, run_id: UUID, done: RunCompletion) -> str:
        """Write the answer unless the run was cancelled meanwhile."""

        def work(connection: Connection) -> str:
            if not self._owned(connection, fence):
                return "lost"
            row = first(
                execute(
                    connection,
                    """SELECT status, cancel_requested_at IS NOT NULL,
                        conversation_id, sequence
                    FROM rag_query_runs WHERE run_id = %(run)s::uuid FOR UPDATE""",
                    {"run": run_id},
                )
            )
            status, cancelled, conversation_id, sequence = TypeAdapter(
                tuple[str, bool, UUID, int]
            ).validate_python(row)
            if status != RunStatus.RUNNING.value or cancelled:
                self._cancel_running(connection, run_id)
                _ = fenced_update(
                    connection,
                    fence,
                    "status = 'CANCELLED', failure_code = 'cancelled'",
                )
                return "cancelled"
            _ = execute(
                connection,
                """UPDATE rag_query_runs SET status = 'completed',
                    outcome = %(outcome)s, abstention_reason = %(abstention)s,
                    answer = %(answer)s::jsonb, effective_scope = %(scope)s::jsonb,
                    as_of = %(as_of)s, source_versions = %(versions)s::jsonb,
                    usage = %(usage)s::jsonb, retrieval_query = %(query)s,
                    history_message_ids = %(history)s::jsonb,
                    finished_at = clock_timestamp()
                WHERE run_id = %(run)s::uuid""",
                {
                    "run": run_id,
                    "outcome": done.outcome.value,
                    "abstention": done.abstention_reason,
                    "answer": json.dumps(done.answer, default=str),
                    "scope": json.dumps(done.effective_scope, default=str),
                    "as_of": done.as_of,
                    "versions": json.dumps(done.source_versions),
                    "usage": json.dumps(done.usage, default=str),
                    "query": done.retrieval_query,
                    "history": json.dumps(list(done.history_message_ids)),
                },
            )
            _ = execute(
                connection,
                """INSERT INTO rag_messages (message_id, conversation_id, run_id,
                    role, sequence, content)
                VALUES (%(message)s::uuid, %(id)s::uuid, %(run)s::uuid,
                    'assistant', %(seq)s, %(content)s)""",
                {
                    "message": uuid4(),
                    "id": conversation_id,
                    "run": run_id,
                    "seq": sequence + 1,
                    "content": done.assistant_text,
                },
            )
            for citation in done.citations:
                _ = execute(
                    connection,
                    """INSERT INTO rag_run_citations ("""
                    + CITATION_COLUMNS
                    + """, run_id) VALUES (%(citation_id)s::uuid, %(ordinal)s,
                        %(claim_id)s::uuid, %(article_id)s::uuid, %(content_hash)s,
                        %(span_id)s::uuid, %(start)s, %(end)s, %(excerpt)s,
                        %(fingerprint)s, %(title)s, %(name)s, %(domain)s, %(url)s,
                        %(published)s, %(run)s::uuid)""",
                    {
                        "citation_id": citation.citation_id,
                        "ordinal": citation.ordinal,
                        "claim_id": citation.claim_id,
                        "article_id": citation.article_id,
                        "content_hash": citation.content_hash,
                        "span_id": citation.span_id,
                        "start": citation.start_offset,
                        "end": citation.end_offset,
                        "excerpt": citation.excerpt,
                        "fingerprint": citation.span_fingerprint,
                        "title": citation.source_title,
                        "name": citation.source_name,
                        "domain": citation.source_domain,
                        "url": citation.source_url,
                        "published": citation.source_published_at,
                        "run": run_id,
                    },
                )
            _ = fenced_update(connection, fence, "status = 'DONE'")
            return "completed"

        return self._tx(work)

    @staticmethod
    def _cancel_running(connection: Connection, run_id: UUID) -> None:
        _ = execute(
            connection,
            """UPDATE rag_query_runs SET status = 'cancelled',
                finished_at = clock_timestamp(),
                cancel_requested_at = coalesce(cancel_requested_at, clock_timestamp())
            WHERE run_id = %(run)s::uuid AND status IN ('queued', 'running')""",
            {"run": run_id},
        )

    def fail(
        self, fence: JobFence | None, run_id: UUID, code: str, reason: str | None
    ) -> bool:
        """Failed run (and FAILED job when the fence still holds)."""

        def work(connection: Connection) -> bool:
            if fence is not None and not self._owned(connection, fence):
                return False
            row = first(
                execute(
                    connection,
                    """UPDATE rag_query_runs SET status = 'failed',
                        failure_code = %(code)s, failure_reason = %(reason)s,
                        finished_at = clock_timestamp()
                    WHERE run_id = %(run)s::uuid AND status IN ('queued', 'running')
                    RETURNING 1""",
                    {"run": run_id, "code": code, "reason": reason},
                )
            )
            if fence is not None:
                _ = fenced_update(
                    connection,
                    fence,
                    "status = 'FAILED', failure_code = %(code)s",
                    code=code,
                )
            return row is not None

        return self._tx(work)

    def finish_cancelled(self, fence: JobFence, run_id: UUID) -> bool:
        """Cooperative cancel acknowledged by the worker."""

        def work(connection: Connection) -> bool:
            if not self._owned(connection, fence):
                return False
            self._cancel_running(connection, run_id)
            _ = fenced_update(
                connection, fence, "status = 'CANCELLED', failure_code = 'cancelled'"
            )
            return True

        return self._tx(work)
