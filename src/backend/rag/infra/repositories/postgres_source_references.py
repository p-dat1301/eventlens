"""PostgreSQL source references shared by every API process (D37).

The raw token is returned to the client once and never stored; rows are keyed
by its SHA-256. References expire after ``ttl_seconds``; expired rows are
purged opportunistically when new references are issued.
"""

import secrets
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import DateTime, Engine, delete, func, insert, select

from backend.rag.application.ports.source_references import SourceReference
from backend.rag.domain.access import CallerScope
from backend.rag.domain.retrieval import RetrievalStorageError
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import transaction
from backend.rag.infra.postgres.models import SourceReferenceRow as Ref
from backend.shared.contracts.evidence import EvidenceSpan

DEFAULT_TTL_SECONDS = 86_400
_ROW = TypeAdapter(tuple[str, str, UUID, str, UUID, int, int, str, str])


def _digest(token: str) -> str:
    return sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PostgresSourceReferences:
    """Opaque, caller-bound, expiring references in ``rag_source_references``."""

    engine: Engine
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def issue(self, caller: CallerScope, span: EvidenceSpan) -> str:
        """Insert a new reference; the raw token is never persisted."""
        token = secrets.token_urlsafe(24)
        now = func.clock_timestamp(type_=DateTime(timezone=True))
        try:
            with transaction(self.engine) as connection:
                _ = connection.execute(delete(Ref).where(Ref.expires_at <= now))
                _ = connection.execute(
                    insert(Ref).values(
                        token_sha256=_digest(token),
                        principal_id=caller.principal_id,
                        scope_id=caller.scope_id,
                        article_id=span.article_id,
                        content_hash=span.content_hash,
                        span_id=span.span_id,
                        start_offset=span.start_offset,
                        end_offset=span.end_offset,
                        excerpt=span.excerpt,
                        span_fingerprint=span.span_fingerprint,
                        expires_at=now + timedelta(seconds=self.ttl_seconds),
                    )
                )
        except (StorageUnavailableError, StorageOperationError) as error:
            raise RetrievalStorageError from error
        return token

    def resolve(self, token: str) -> SourceReference | None:
        """Return an unexpired reference, or ``None``."""
        query = select(
            Ref.principal_id,
            Ref.scope_id,
            Ref.article_id,
            Ref.content_hash,
            Ref.span_id,
            Ref.start_offset,
            Ref.end_offset,
            Ref.excerpt,
            Ref.span_fingerprint,
        ).where(
            Ref.token_sha256 == _digest(token),
            Ref.expires_at > func.clock_timestamp(),
        )
        try:
            with transaction(self.engine) as connection:
                row = connection.execute(query).first()
                found = None if row is None else tuple(row)
        except (StorageUnavailableError, StorageOperationError) as error:
            raise RetrievalStorageError from error
        if found is None:
            return None
        principal, scope, article, digest, span_id, start, end, excerpt, mark = (
            _ROW.validate_python(found)
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
