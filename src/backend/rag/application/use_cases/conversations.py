"""Sessions, conversations and run submission (no LLM, no LangGraph here).

The session owner is the only identity trusted for ownership; nothing in a
request body or header other than the opaque session token is consulted.
Anonymous demo sessions isolate conversations between browsers; they are not
an ACL for a private corpus (the corpus caller stays the configured scope).
"""

# ruff: noqa: EM101

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.catalog import ArticleCatalogStore
from backend.rag.application.ports.conversations import (
    ConversationStore,
    NewRun,
    SessionStore,
    SubmitResult,
)
from backend.rag.application.ports.source_references import SourceReferenceRegistry
from backend.rag.application.ports.synthesis import CurrentArticleReader
from backend.rag.application.use_cases.inspect_source import (
    SourceAccessDeniedError,
    SourceStaleError,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.catalog import CatalogStorageError
from backend.rag.domain.conversation import (
    MAX_IDEMPOTENCY_KEY,
    MAX_TITLE_CHARS,
    CitationNotFoundError,
    Conversation,
    ConversationNotFoundError,
    ConversationScope,
    ConversationStorageError,
    Message,
    ModelUnavailableError,
    QueryRun,
    RunNotFoundError,
    ScopeInvalidError,
    ScopeType,
    SessionPrincipal,
    SessionRequiredError,
    validate_question,
)
from backend.shared.contracts.evidence import EvidenceSpan

SESSION_TTL_SECONDS: Final = 7 * 24 * 3600
MAX_SESSION_TOKEN: Final = 128
RUN_MAX_ATTEMPTS: Final = 3
RUN_QUEUE_DEADLINE_SECONDS: Final = 600.0
MAX_PAGE: Final = 50
_IDEMPOTENCY_KEY: Final = re.compile(r"^[\x21-\x7e]{1,200}$")


def token_sha256(token: str) -> str:
    """Storage key of a session token."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session token (returned once, never stored in clear)."""

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Sessions:
    """Server-side demo sessions with a random owner each."""

    store: SessionStore
    ttl_seconds: int = SESSION_TTL_SECONDS
    tokens: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(32))

    def create(self) -> IssuedSession:
        """New owner, new token."""
        token = self.tokens()
        expires = self.store.create(token_sha256(token), uuid4(), self.ttl_seconds)
        return IssuedSession(token, expires)

    def resolve(self, token: str | None) -> SessionPrincipal:
        """Verified owner or ``SessionRequiredError``."""
        if not token or len(token) > MAX_SESSION_TOKEN:
            raise SessionRequiredError
        principal = self.store.resolve(token_sha256(token))
        if principal is None:
            raise SessionRequiredError
        return principal

    def revoke(self, token: str) -> None:
        """Log out."""
        if token and len(token) <= MAX_SESSION_TOKEN:
            self.store.revoke(token_sha256(token))


def _encode_cursor(conversation: Conversation) -> str:
    raw = f"{conversation.created_at.isoformat()}|{conversation.conversation_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, _, conversation_id = raw.partition("|")
        return datetime.fromisoformat(at), UUID(conversation_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ScopeInvalidError("invalid_cursor") from None


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """Owner's conversations, newest first."""

    items: tuple[Conversation, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class IssuedSourceRef:
    """Fresh viewer reference for a stored citation."""

    source_ref: str


@dataclass(frozen=True, slots=True)
class Conversations:
    """Owner-checked conversation use cases."""

    store: ConversationStore
    catalog: ArticleCatalogStore
    sources: CurrentArticleReader
    references: SourceReferenceRegistry
    access: AccessPolicy
    caller: CallerScope
    answers_enabled: bool

    def create(
        self, principal: SessionPrincipal, scope: ConversationScope, title: str | None
    ) -> Conversation:
        """Validate the scope against current data, then persist it."""
        if title is not None and len(title) > MAX_TITLE_CHARS:
            raise ScopeInvalidError("title_too_long")
        try:
            if scope.scope_type is ScopeType.ARTICLES:
                ids = tuple(p.article_id for p in scope.articles)
                current = self.catalog.current_versions(ids)
                if set(current) != set(ids):
                    raise ScopeInvalidError("scope_article_unavailable")
                if any(
                    p.article_version_id not in {None, current[p.article_id]}
                    for p in scope.articles
                ):
                    raise ScopeInvalidError("scope_stale")
            elif scope.filters is not None:
                _ = self.catalog.resolve_scope(scope.filters, 0)
        except CatalogStorageError:
            raise ConversationStorageError from None
        return self.store.create(
            principal.owner_id, uuid4(), title, scope.canonical(), scope.digest()
        )

    def list(
        self, principal: SessionPrincipal, limit: int, cursor: str | None
    ) -> ConversationPage:
        """Keyset page of the owner's conversations."""
        if not 1 <= limit <= MAX_PAGE:
            raise ScopeInvalidError("invalid_limit")
        before = None if cursor is None else _decode_cursor(cursor)
        found = self.store.list(principal.owner_id, limit + 1, before)
        items = found[:limit]
        more = len(found) > limit
        return ConversationPage(
            items, _encode_cursor(items[-1]) if more and items else None
        )

    def get(self, principal: SessionPrincipal, conversation_id: UUID) -> Conversation:
        """Owned conversation or 404."""
        found = self.store.get(principal.owner_id, conversation_id)
        if found is None:
            raise ConversationNotFoundError
        return found

    def messages(
        self,
        principal: SessionPrincipal,
        conversation_id: UUID,
        after_sequence: int,
        limit: int,
    ) -> tuple[Conversation, tuple[Message, ...]]:
        """Conversation header plus its messages after ``after_sequence``."""
        if not 1 <= limit <= MAX_PAGE * 2 or after_sequence < 0:
            raise ScopeInvalidError("invalid_limit")
        conversation = self.get(principal, conversation_id)
        return conversation, self.store.messages(
            principal.owner_id, conversation_id, after_sequence, limit
        )

    def submit(
        self,
        principal: SessionPrincipal,
        conversation_id: UUID,
        question: str,
        idempotency_key: str | None,
    ) -> SubmitResult:
        """Message + run + job in one transaction; 202 semantics."""
        text = validate_question(question)
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.match(idempotency_key):
            raise ScopeInvalidError("invalid_idempotency_key")
        if not self.answers_enabled:
            raise ModelUnavailableError("provider_not_configured")
        body = json.dumps({"question": text}, sort_keys=True, ensure_ascii=False)
        return self.store.submit(
            principal.owner_id,
            conversation_id,
            NewRun(
                run_id=uuid4(),
                user_message_id=uuid4(),
                job_id=uuid4(),
                question=text,
                idempotency_key=None
                if idempotency_key is None
                else idempotency_key[:MAX_IDEMPOTENCY_KEY],
                request_sha256=hashlib.sha256(body.encode()).hexdigest(),
                max_attempts=RUN_MAX_ATTEMPTS,
                deadline_seconds=RUN_QUEUE_DEADLINE_SECONDS,
            ),
        )

    def run(self, principal: SessionPrincipal, run_id: UUID) -> QueryRun:
        """Owned run or 404."""
        found = self.store.run(principal.owner_id, run_id)
        if found is None:
            raise RunNotFoundError
        return found

    def cancel(self, principal: SessionPrincipal, run_id: UUID) -> QueryRun:
        """Cooperative cancel; returns the real current state."""
        found = self.store.request_cancel(principal.owner_id, run_id)
        if found is None:
            raise RunNotFoundError
        return found

    def delete(self, principal: SessionPrincipal, conversation_id: UUID) -> None:
        """Soft delete (runs cancelled); 404 when not owned."""
        if not self.store.delete(principal.owner_id, conversation_id):
            raise ConversationNotFoundError

    def source_ref(
        self, principal: SessionPrincipal, run_id: UUID, citation_id: UUID
    ) -> IssuedSourceRef:
        """Re-check access and the cited revision, then issue a fresh ref.

        A revision that changed or was removed is ``source_stale`` (410); the
        stored citation is never re-pointed at newer content.
        """
        citation = self.store.citation(principal.owner_id, run_id, citation_id)
        if citation is None:
            raise CitationNotFoundError
        if not self.access.allows(self.caller):
            raise SourceAccessDeniedError
        current = self.sources.current_articles((citation.article_id,))
        if not self.access.allows(self.caller):
            raise SourceAccessDeniedError
        document = next(
            (
                s.document
                for s in current
                if s.document.article_id == citation.article_id
            ),
            None,
        )
        if document is None or document.content_hash != citation.content_hash:
            raise SourceStaleError
        excerpt = document.normalized_content[
            citation.start_offset : citation.end_offset
        ]
        if excerpt != citation.excerpt:
            raise SourceStaleError
        span = EvidenceSpan(
            span_id=citation.span_id,
            article_id=citation.article_id,
            content_hash=citation.content_hash,
            start_offset=citation.start_offset,
            end_offset=citation.end_offset,
            excerpt=citation.excerpt,
            span_fingerprint=citation.span_fingerprint,
            offset_basis="unicode_codepoint",
        )
        return IssuedSourceRef(self.references.issue(self.caller, span))
