"""PostgreSQL adapters of the search platform (revision 0002).

Outbox claims use ``FOR UPDATE SKIP LOCKED`` and a fresh claim token; ack,
retry and dead-letter writes match that token, so a worker whose lease
expired cannot complete an event another worker re-claimed. Every method is
one short transaction; none is held across an index or embedding call.
"""

# ruff: noqa: S608 - SQL is built from reviewed constant fragments; values are bound.

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from threading import Lock
from typing import LiteralString, final
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine

from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from backend.rag.domain.search_embedding import (
    SearchEmbeddingIdentity,
    checked_vector,
)
from backend.rag.domain.search_index import (
    GenerationStatus,
    IndexGeneration,
    RevisionDigest,
)
from backend.rag.infra.postgres.engine import execute, first, rows, transaction
from backend.rag.infra.retrieval.postgres_retrieval import VALID_METADATA
from backend.shared.contracts import ArticleEnrichment
from backend.shared.contracts.enrichment import EnrichmentStatus

_EVENT_ROWS = TypeAdapter(
    list[tuple[int, str, UUID | None, str | None, int | None, int, UUID, datetime]]
)
_GENERATION_ROW = TypeAdapter(
    tuple[int, str, str, str | None, str | None, int | None, str]
)
_REVISION_ROWS = TypeAdapter(list[tuple[UUID, str | None, bool, str | None, int]])
_DIGEST_ROWS = TypeAdapter(list[tuple[UUID, str]])
_VECTOR_ROWS = TypeAdapter(list[tuple[str, int, list[float]]])
_COUNT_ROWS = TypeAdapter(list[tuple[str, int]])


class GenerationStateError(RuntimeError):
    """Activation of a generation that is not BUILDING (or does not exist)."""

    def __init__(self, generation_id: int) -> None:
        """Keep the generation ID."""
        super().__init__(f"search generation {generation_id} is not BUILDING")


def _generation(row: object) -> IndexGeneration:
    gid, name, chunk, identity, model, dims, status = _GENERATION_ROW.validate_python(
        row
    )
    return IndexGeneration(
        generation_id=gid,
        index_name=name,
        chunk_version=chunk,
        embedding_identity=identity,
        embedding_model=model,
        embedding_dimensions=dims,
        status=GenerationStatus(status),
    )


def _metadata(text: str | None, document: ArticleDocument) -> ArticleEnrichment | None:
    if text is None:
        return None
    try:
        metadata = ArticleEnrichment.model_validate_json(text)
    except ValidationError:
        return None
    if (
        metadata.status is not EnrichmentStatus.READY
        or metadata.article_id != document.article_id
        or metadata.content_hash != document.content_hash
    ):
        return None
    return metadata


@final
class PostgresOutboxStore:
    """``OutboxStore`` over ``rag_search_outbox``."""

    def __init__(self, engine: Engine) -> None:
        """Use the process Engine."""
        self._engine = engine

    def claim(self, limit: int, lease_seconds: int) -> tuple[OutboxEvent, ...]:
        """Claim due events (PENDING, or PROCESSING with an expired lease)."""
        token = uuid4()
        with transaction(self._engine) as connection:
            found = rows(
                execute(
                    connection,
                    """
WITH due AS (
    SELECT event_id FROM rag_search_outbox
    WHERE (status = 'PENDING' AND next_attempt_at <= clock_timestamp())
       OR (status = 'PROCESSING' AND lease_expires_at <= clock_timestamp())
    ORDER BY event_id
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE rag_search_outbox o
SET status = 'PROCESSING', attempts = o.attempts + 1, claim_token = %(token)s,
    lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s)
FROM due WHERE o.event_id = due.event_id
RETURNING o.event_id, o.event_type, o.article_id, o.content_hash,
          o.generation_id, o.attempts, o.claim_token, o.created_at
""",
                    {"limit": limit, "token": token, "lease": lease_seconds},
                )
            )
        events = [
            OutboxEvent(
                event_id=eid,
                event_type=OutboxEventType(kind),
                article_id=article,
                content_hash=content_hash,
                generation_id=generation,
                attempts=attempts,
                claim_token=claim,
                created_at=created,
            )
            for (
                eid,
                kind,
                article,
                content_hash,
                generation,
                attempts,
                claim,
                created,
            ) in _EVENT_ROWS.validate_python(found)
        ]
        return tuple(sorted(events, key=lambda event: event.event_id))

    def _fenced(self, sql: LiteralString, event: OutboxEvent, **params: object) -> bool:
        with transaction(self._engine) as connection:
            result = execute(
                connection,
                sql,
                {"id": event.event_id, "token": event.claim_token, **params},
            )
            return result.rowcount == 1

    def complete(self, events: Sequence[OutboxEvent]) -> int:
        """DONE for each event still held with its claim token."""
        if not events:
            return 0
        with transaction(self._engine) as connection:
            result = execute(
                connection,
                """
UPDATE rag_search_outbox o
SET status = 'DONE', completed_at = clock_timestamp(), lease_expires_at = NULL,
    last_error = NULL
FROM unnest(%(ids)s::bigint[], %(tokens)s::uuid[]) AS claimed(id, token)
WHERE o.event_id = claimed.id AND o.claim_token = claimed.token
  AND o.status = 'PROCESSING'
""",
                {
                    "ids": [event.event_id for event in events],
                    "tokens": [event.claim_token for event in events],
                },
            )
            return result.rowcount

    def retry(self, event: OutboxEvent, error_code: str, delay_seconds: float) -> bool:
        """Back to PENDING after the backoff delay."""
        return self._fenced(
            """
UPDATE rag_search_outbox
SET status = 'PENDING', last_error = %(code)s, lease_expires_at = NULL,
    next_attempt_at = clock_timestamp() + make_interval(secs => %(delay)s)
WHERE event_id = %(id)s AND claim_token = %(token)s AND status = 'PROCESSING'
""",
            event,
            code=error_code[:200],
            delay=delay_seconds,
        )

    def fail(self, event: OutboxEvent, error_code: str) -> bool:
        """Dead-letter (FAILED)."""
        return self._fenced(
            """
UPDATE rag_search_outbox
SET status = 'FAILED', last_error = %(code)s, lease_expires_at = NULL,
    completed_at = clock_timestamp()
WHERE event_id = %(id)s AND claim_token = %(token)s AND status = 'PROCESSING'
""",
            event,
            code=error_code[:200],
        )

    def counts(self) -> dict[OutboxStatus, int]:
        """Events per status."""
        with transaction(self._engine, read_only=True) as connection:
            found = rows(
                execute(
                    connection,
                    "SELECT status, count(*) FROM rag_search_outbox GROUP BY status",
                )
            )
        counted = dict(_COUNT_ROWS.validate_python(found))
        return {status: counted.get(status.value, 0) for status in OutboxStatus}

    def oldest_pending(self) -> datetime | None:
        """Creation time of the oldest PENDING event."""
        with transaction(self._engine, read_only=True) as connection:
            row = first(
                execute(
                    connection,
                    """SELECT min(created_at) FROM rag_search_outbox
WHERE status = 'PENDING'""",
                )
            )
        value = None if row is None else row[0]
        return value if isinstance(value, datetime) else None

    def requeue_failed(self) -> int:
        """FAILED -> PENDING with attempts reset (operator action)."""
        with transaction(self._engine) as connection:
            return execute(
                connection,
                """
UPDATE rag_search_outbox
SET status = 'PENDING', attempts = 0, next_attempt_at = clock_timestamp(),
    completed_at = NULL, claim_token = NULL
WHERE status = 'FAILED'
""",
            ).rowcount


# Constant fragments only (VALID_METADATA is reviewed SQL); values are bound.
_PROJECTION_SQL: LiteralString = (
    """
WITH valid AS (
"""
    + VALID_METADATA
    + """
    AND cur.article_id = ANY(%(ids)s::uuid[])
), agg AS (
    SELECT article_id, count(*) AS publications,
           (array_agg(metadata::text))[1] AS metadata
    FROM valid GROUP BY article_id
)
SELECT cur.article_id, d.document_json::text, cur.tombstoned_at IS NOT NULL,
       CASE WHEN agg.publications = 1 THEN agg.metadata END,
       (SELECT coalesce(max(o.event_id), 0) FROM rag_search_outbox o
        WHERE o.article_id = cur.article_id)
FROM rag_article_current cur
JOIN rag_article_documents d
    ON d.article_id = cur.article_id AND d.content_hash = cur.content_hash
LEFT JOIN agg ON agg.article_id = cur.article_id
WHERE cur.article_id = ANY(%(ids)s::uuid[])
"""
)


@final
class PostgresProjectionSource:
    """``SearchProjectionSource``: current revisions straight from PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        """Use the process Engine."""
        self._engine = engine

    def _revisions(self, ids: Sequence[UUID]) -> dict[UUID, ProjectionRevision]:
        if not ids:
            return {}
        with transaction(self._engine, read_only=True) as connection:
            found = rows(execute(connection, _PROJECTION_SQL, {"ids": list(ids)}))
        out: dict[UUID, ProjectionRevision] = {}
        for (
            article_id,
            text,
            tombstoned,
            metadata,
            version,
        ) in _REVISION_ROWS.validate_python(found):
            document = (
                None if text is None else ArticleDocument.model_validate_json(text)
            )
            out[article_id] = ProjectionRevision(
                article_id=article_id,
                document=document,
                tombstoned=tombstoned,
                metadata=(None if document is None else _metadata(metadata, document)),
                version=version,
            )
        return out

    def revision(self, article_id: UUID) -> ProjectionRevision:
        """Current state; unknown articles come back with ``document=None``."""
        found = self._revisions([article_id]).get(article_id)
        if found is not None:
            return found
        with transaction(self._engine, read_only=True) as connection:
            row = first(
                execute(
                    connection,
                    """SELECT coalesce(max(event_id), 0) FROM rag_search_outbox
WHERE article_id = %(id)s""",
                    {"id": article_id},
                )
            )
        version = 0 if row is None else int(str(row[0]))
        return ProjectionRevision(
            article_id=article_id,
            document=None,
            tombstoned=False,
            metadata=None,
            version=version,
        )

    def current_page(
        self, after: UUID | None, limit: int
    ) -> tuple[ProjectionRevision, ...]:
        """Untombstoned current revisions after ``after`` (keyset by article)."""
        with transaction(self._engine, read_only=True) as connection:
            ids = [
                UUID(str(row[0]))
                for row in rows(
                    execute(
                        connection,
                        """
SELECT article_id FROM rag_article_current
WHERE tombstoned_at IS NULL
  AND (%(after)s::uuid IS NULL OR article_id > %(after)s::uuid)
ORDER BY article_id LIMIT %(limit)s
""",
                        {"after": after, "limit": limit},
                    )
                )
            ]
        found = self._revisions(ids)
        return tuple(
            found[article_id]
            for article_id in ids
            if article_id in found and not found[article_id].tombstoned
        )

    def current_digests(self) -> tuple[RevisionDigest, ...]:
        """Every untombstoned current ``(article_id, content_hash)``."""
        with transaction(self._engine, read_only=True) as connection:
            found = rows(
                execute(
                    connection,
                    """SELECT article_id, content_hash FROM rag_article_current
WHERE tombstoned_at IS NULL ORDER BY article_id""",
                )
            )
        return tuple(
            RevisionDigest(article_id, content_hash, -1)
            for article_id, content_hash in _DIGEST_ROWS.validate_python(found)
        )


@final
class PostgresSearchGenerations:
    """``SearchGenerationStore`` over ``rag_search_generations``."""

    def __init__(self, engine: Engine) -> None:
        """Use the process Engine."""
        self._engine = engine

    def _one(
        self, sql: LiteralString, params: dict[str, object]
    ) -> IndexGeneration | None:
        with transaction(self._engine, read_only=True) as connection:
            row = first(execute(connection, sql, params))
        return None if row is None else _generation(tuple(row))

    def active(self) -> IndexGeneration | None:
        """The ACTIVE generation."""
        return self._one(
            """SELECT generation_id, index_name, chunk_version, embedding_identity,
    embedding_model, embedding_dimensions, status
FROM rag_search_generations WHERE status = 'ACTIVE'""",
            {},
        )

    def get(self, generation_id: int) -> IndexGeneration | None:
        """One generation."""
        return self._one(
            """SELECT generation_id, index_name, chunk_version, embedding_identity,
    embedding_model, embedding_dimensions, status
FROM rag_search_generations WHERE generation_id = %(id)s""",
            {"id": generation_id},
        )

    def writable(self) -> tuple[IndexGeneration, ...]:
        """ACTIVE and BUILDING generations."""
        with transaction(self._engine, read_only=True) as connection:
            found = rows(
                execute(
                    connection,
                    """SELECT generation_id, index_name, chunk_version,
    embedding_identity, embedding_model, embedding_dimensions, status
FROM rag_search_generations WHERE status IN ('ACTIVE', 'BUILDING')
ORDER BY generation_id""",
                )
            )
        return tuple(_generation(tuple(row)) for row in found)

    def create(
        self,
        chunk_version: str,
        embedding: SearchEmbeddingIdentity | None,
        index_prefix: str,
    ) -> IndexGeneration:
        """New BUILDING generation; the index name embeds its ID."""
        with transaction(self._engine) as connection:
            row = first(
                execute(
                    connection,
                    """
INSERT INTO rag_search_generations (
    index_name, chunk_version, embedding_identity, embedding_model,
    embedding_dimensions, embedding_revision)
VALUES (%(prefix)s || '-pending-' || gen_random_uuid(), %(chunk)s, %(identity)s,
        %(model)s, %(dims)s, %(revision)s)
RETURNING generation_id
""",
                    {
                        "prefix": index_prefix,
                        "chunk": chunk_version,
                        "identity": None if embedding is None else embedding.key,
                        "model": None if embedding is None else embedding.model,
                        "dims": None if embedding is None else embedding.dimensions,
                        "revision": None if embedding is None else embedding.revision,
                    },
                )
            )
            assert row is not None  # noqa: S101 - INSERT ... RETURNING
            gid = int(str(row[0]))
            named = first(
                execute(
                    connection,
                    """UPDATE rag_search_generations SET index_name = %(name)s
WHERE generation_id = %(id)s
RETURNING generation_id, index_name, chunk_version, embedding_identity,
    embedding_model, embedding_dimensions, status""",
                    {"name": f"{index_prefix}-g{gid}-{uuid4().hex[:8]}", "id": gid},
                )
            )
            assert named is not None  # noqa: S101 - row exists in this transaction
            return _generation(tuple(named))

    def activate(self, generation_id: int) -> IndexGeneration:
        """ACTIVE + retire the previous + outbox event, one transaction."""
        with transaction(self._engine) as connection:
            target = first(
                execute(
                    connection,
                    """SELECT status FROM rag_search_generations
WHERE generation_id = %(id)s FOR UPDATE""",
                    {"id": generation_id},
                )
            )
            if target is None or target[0] != GenerationStatus.BUILDING.value:
                raise GenerationStateError(generation_id)
            _ = execute(
                connection,
                """UPDATE rag_search_generations SET status = 'RETIRED'
WHERE status = 'ACTIVE'""",
            )
            row = first(
                execute(
                    connection,
                    """UPDATE rag_search_generations
SET status = 'ACTIVE', activated_at = clock_timestamp()
WHERE generation_id = %(id)s
RETURNING generation_id, index_name, chunk_version, embedding_identity,
    embedding_model, embedding_dimensions, status""",
                    {"id": generation_id},
                )
            )
            _ = execute(
                connection,
                """INSERT INTO rag_search_outbox (event_type, generation_id)
VALUES ('embedding_generation_activated', %(id)s)""",
                {"id": generation_id},
            )
            assert row is not None  # noqa: S101 - locked above
            return _generation(tuple(row))

    def mark_failed(self, generation_id: int) -> None:
        """BUILDING -> FAILED (never ACTIVE afterwards)."""
        with transaction(self._engine) as connection:
            _ = execute(
                connection,
                """UPDATE rag_search_generations SET status = 'FAILED'
WHERE generation_id = %(id)s AND status = 'BUILDING'""",
                {"id": generation_id},
            )


@final
class PostgresEmbeddingCache:
    """``EmbeddingCache`` over ``rag_search_embedding_cache``."""

    def __init__(self, engine: Engine) -> None:
        """Use the process Engine."""
        self._engine = engine

    def get_many(
        self, text_hashes: Sequence[str], identity: SearchEmbeddingIdentity
    ) -> dict[str, tuple[float, ...]]:
        """Cached vectors of this identity (dimension re-checked)."""
        if not text_hashes:
            return {}
        with transaction(self._engine, read_only=True) as connection:
            found = rows(
                execute(
                    connection,
                    """
SELECT text_hash, embedding_dimensions, embedding
FROM rag_search_embedding_cache
WHERE embedding_identity = %(identity)s AND text_hash = ANY(%(hashes)s::text[])
""",
                    {"identity": identity.key, "hashes": list(text_hashes)},
                )
            )
        return {
            key: checked_vector(tuple(values), identity.dimensions)
            for key, dims, values in _VECTOR_ROWS.validate_python(found)
            if dims == identity.dimensions
        }

    def put_many(
        self,
        vectors: Sequence[tuple[str, tuple[float, ...]]],
        identity: SearchEmbeddingIdentity,
    ) -> int:
        """Insert missing vectors; existing keys are left untouched."""
        written = 0
        with transaction(self._engine) as connection:
            for key, values in vectors:
                result = execute(
                    connection,
                    """
INSERT INTO rag_search_embedding_cache (
    text_hash, embedding_identity, embedding_dimensions, embedding)
VALUES (%(hash)s, %(identity)s, %(dims)s, %(values)s::double precision[])
ON CONFLICT (text_hash, embedding_identity) DO NOTHING
""",
                    {
                        "hash": key,
                        "identity": identity.key,
                        "dims": identity.dimensions,
                        "values": list(checked_vector(values, identity.dimensions)),
                    },
                )
                written += result.rowcount
        return written


@final
class ActiveGenerationCache:
    """Process-local TTL cache of the ACTIVE generation (one read per TTL)."""

    def __init__(
        self,
        generations: PostgresSearchGenerations,
        ttl_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wrap the generation store."""
        self._generations = generations
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._value: IndexGeneration | None = None
        self._expires = float("-inf")

    def __call__(self) -> IndexGeneration | None:
        """The cached ACTIVE generation (refreshed after the TTL)."""
        with self._lock:
            now = self._clock()
            if now >= self._expires:
                self._value = self._generations.active()
                self._expires = now + self._ttl
            return self._value
