"""PostgreSQL source references shared by every API process (D37).

The raw token is returned to the client once and never stored; rows are keyed
by its SHA-256. References expire after ``ttl_seconds``; expired rows are
purged opportunistically when new references are issued.
"""

import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import LiteralString
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow
from pydantic import TypeAdapter

from eventlens_contracts.evidence import EvidenceSpan
from eventlens_rag.adapters.connection import StorageUnavailableError, connect
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.retrieval import RetrievalStorageError
from eventlens_rag.ports.source_references import SourceReference

DEFAULT_TTL_SECONDS = 86_400
_ROW = TypeAdapter(tuple[str, str, UUID, str, UUID, int, int, str, str])
_PURGE_SQL: LiteralString = (
    "DELETE FROM rag_source_references WHERE expires_at <= clock_timestamp()"
)
_INSERT_SQL: LiteralString = """INSERT INTO rag_source_references (
    token_sha256, principal_id, scope_id, article_id, content_hash, span_id,
    start_offset, end_offset, excerpt, span_fingerprint, expires_at)
VALUES (%(token)s, %(principal)s, %(scope)s, %(article)s, %(hash)s, %(span)s,
    %(start)s, %(end)s, %(excerpt)s, %(fingerprint)s,
    clock_timestamp() + make_interval(secs => %(ttl)s))"""
_SELECT_SQL: LiteralString = """SELECT principal_id, scope_id, article_id,
    content_hash, span_id, start_offset, end_offset, excerpt, span_fingerprint
FROM rag_source_references
WHERE token_sha256 = %(token)s AND expires_at > clock_timestamp()"""


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PostgresSourceReferences:
    """Opaque, caller-bound, expiring references in ``rag_source_references``."""

    database_url: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def _run(
        self, statements: tuple[tuple[LiteralString, dict[str, object]], ...]
    ) -> list[TupleRow]:
        rows: list[TupleRow] = []
        try:
            with connect(self.database_url) as connection:
                for query, params in statements:
                    cursor = connection.execute(query, params)
                    if cursor.description is not None:
                        rows = cursor.fetchall()
        except StorageUnavailableError as error:
            raise RetrievalStorageError from error
        except psycopg.Error as error:
            raise RetrievalStorageError from error
        return rows

    def issue(self, caller: CallerScope, span: EvidenceSpan) -> str:
        """Insert a new reference; the raw token is never persisted."""
        token = secrets.token_urlsafe(24)
        _ = self._run(
            (
                (_PURGE_SQL, {}),
                (
                    _INSERT_SQL,
                    {
                        "token": _digest(token),
                        "principal": caller.principal_id,
                        "scope": caller.scope_id,
                        "article": span.article_id,
                        "hash": span.content_hash,
                        "span": span.span_id,
                        "start": span.start_offset,
                        "end": span.end_offset,
                        "excerpt": span.excerpt,
                        "fingerprint": span.span_fingerprint,
                        "ttl": self.ttl_seconds,
                    },
                ),
            )
        )
        return token

    def resolve(self, token: str) -> SourceReference | None:
        """Return an unexpired reference, or ``None``."""
        rows = self._run(((_SELECT_SQL, {"token": _digest(token)}),))
        if not rows:
            return None
        principal, scope, article, digest, span_id, start, end, excerpt, mark = (
            _ROW.validate_python(rows[0])
        )
        span = EvidenceSpan.model_validate(
            {
                "span_id": span_id,
                "article_id": article,
                "content_hash": digest,
                "start_offset": start,
                "end_offset": end,
                "excerpt": excerpt,
                "span_fingerprint": mark,
                "offset_basis": "unicode_codepoint",
            }
        )
        return SourceReference(principal, scope, span)
