"""Conversations, scopes, durable query runs and stored citations.

Scope types:

* ``all``: every article the caller may read;
* ``filtered``: the *whole* set matching a catalog filter definition (same
  semantics as the catalog, never a page of it), re-resolved at each run;
* ``articles``: explicit article IDs, optionally pinned to a version; a
  pinned version that is no longer current fails the run with ``scope_stale``.

A conversation stores the scope *definition*; each run records the effective
scope, ``as_of`` and the revisions used as evidence. Earlier answers are
history, never evidence. Ownership is the server-side session owner.
"""

# ruff: noqa: EM101

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Final, cast
from uuid import UUID

from backend.rag.domain.catalog import CatalogFilters, catalog_filters

MAX_SCOPE_ARTICLES: Final = 20
MAX_FILTERED_SCOPE: Final = 5_000
MAX_QUESTION_CHARS: Final = 2_000
MAX_TITLE_CHARS: Final = 200
MAX_IDEMPOTENCY_KEY: Final = 200
HISTORY_POLICY_ID: Final = "previous-question-concat.v1"
FOLLOW_UP_MAX_WORDS: Final = 12


class ScopeType(StrEnum):
    """Conversation scope kinds."""

    ALL = "all"
    FILTERED = "filtered"
    ARTICLES = "articles"


class RunStatus(StrEnum):
    """Query run lifecycle: queued -> running -> completed|failed|cancelled."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """No further transition."""
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class RunOutcome(StrEnum):
    """Outcome of a completed run (model/storage errors are failures)."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"


class ConversationError(Exception):
    """Typed conversation failure with a stable code (fixed message)."""

    code: ClassVar[str] = "conversation_error"
    status: ClassVar[int] = 400

    def __init__(self, reason: str | None = None) -> None:
        """Optional enum reason (never user content)."""
        super().__init__(self.code)
        self.reason: str | None = reason


class ScopeInvalidError(ConversationError):
    """Scope definition rejected."""

    code: ClassVar[str] = "invalid_scope"


class ConversationNotFoundError(ConversationError):
    """Unknown, deleted or foreign conversation (existence is not revealed)."""

    code: ClassVar[str] = "conversation_not_found"
    status: ClassVar[int] = 404


class RunNotFoundError(ConversationError):
    """Unknown or foreign run."""

    code: ClassVar[str] = "run_not_found"
    status: ClassVar[int] = 404


class CitationNotFoundError(ConversationError):
    """Unknown citation of an owned run."""

    code: ClassVar[str] = "citation_not_found"
    status: ClassVar[int] = 404


class RunInProgressError(ConversationError):
    """MVP: one active run per conversation."""

    code: ClassVar[str] = "run_in_progress"
    status: ClassVar[int] = 409


class IdempotencyConflictError(ConversationError):
    """Same Idempotency-Key, different request body."""

    code: ClassVar[str] = "idempotency_key_reused"
    status: ClassVar[int] = 409


class QuestionInvalidError(ConversationError):
    """Empty or oversized question."""

    code: ClassVar[str] = "invalid_question"


class SessionRequiredError(ConversationError):
    """No valid server-side session."""

    code: ClassVar[str] = "unauthenticated"
    status: ClassVar[int] = 401


class ModelUnavailableError(ConversationError):
    """No answer provider is configured for runs."""

    code: ClassVar[str] = "model_unavailable"
    status: ClassVar[int] = 503


class ConversationStorageError(ConversationError):
    """Conversation storage unavailable (never an empty result)."""

    code: ClassVar[str] = "conversation_storage_unavailable"
    status: ClassVar[int] = 503


@dataclass(frozen=True, slots=True)
class ArticlePin:
    """One article of an ``articles`` scope, optionally version-pinned."""

    article_id: UUID
    article_version_id: str | None


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """Validated scope definition (construct with ``conversation_scope``)."""

    scope_type: ScopeType
    filters: CatalogFilters | None
    articles: tuple[ArticlePin, ...]

    def canonical(self) -> dict[str, object]:
        """Stored JSON definition."""
        return {
            "type": self.scope_type.value,
            "filters": None if self.filters is None else self.filters.canonical(),
            "articles": [
                {
                    "article_id": str(p.article_id),
                    "article_version_id": p.article_version_id,
                }
                for p in self.articles
            ],
        }

    def digest(self) -> str:
        """SHA-256 of the canonical definition."""
        return canonical_sha256(self.canonical())


def canonical_sha256(value: object) -> str:
    """SHA-256 of sorted compact JSON."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def conversation_scope(
    scope_type: ScopeType,
    filters: CatalogFilters | None,
    articles: tuple[ArticlePin, ...],
) -> ConversationScope:
    """Validate the shape of one scope definition."""
    match scope_type:
        case ScopeType.ALL:
            if filters is not None or articles:
                raise ScopeInvalidError("all_takes_no_filters")
        case ScopeType.FILTERED:
            if filters is None or articles:
                raise ScopeInvalidError("filtered_requires_filters")
        case ScopeType.ARTICLES:
            if filters is not None or not 1 <= len(articles) <= MAX_SCOPE_ARTICLES:
                raise ScopeInvalidError("articles_requires_1_to_20_ids")
            if len({p.article_id for p in articles}) != len(articles):
                raise ScopeInvalidError("duplicate_article_id")
    return ConversationScope(scope_type, filters, articles)


def validate_question(question: str) -> str:
    """Trimmed question within bounds."""
    text = question.strip()
    if not text or len(text) > MAX_QUESTION_CHARS:
        raise QuestionInvalidError
    return text


def retrieval_query(question: str, previous_questions: tuple[str, ...]) -> str:
    """History policy ``previous-question-concat.v1``.

    A short follow-up ("còn ở Hà Nội thì sao?") is retrieved together with the
    previous *question* of the conversation. Earlier answers are never used:
    they are not evidence.
    """
    if not previous_questions or len(question.split()) > FOLLOW_UP_MAX_WORDS:
        return question
    combined = f"{previous_questions[-1]}\n{question}"
    return combined[-MAX_QUESTION_CHARS:]


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """Owner established by a server-side session (not a corpus ACL)."""

    owner_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Conversation:
    """Conversation header."""

    conversation_id: UUID
    owner_id: UUID
    title: str | None
    scope: dict[str, object]
    scope_sha256: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredCitation:
    """Citation identity/version/span persisted with a run."""

    citation_id: UUID
    ordinal: int
    claim_id: UUID
    article_id: UUID
    content_hash: str
    span_id: UUID
    start_offset: int
    end_offset: int
    excerpt: str
    span_fingerprint: str
    source_title: str
    source_name: str
    source_domain: str
    source_url: str
    source_published_at: datetime | None


@dataclass(frozen=True, slots=True)
class QueryRun:
    """Durable run record."""

    run_id: UUID
    conversation_id: UUID
    owner_id: UUID
    sequence: int
    question: str
    status: RunStatus
    outcome: RunOutcome | None
    abstention_reason: str | None
    failure_code: str | None
    failure_reason: str | None
    answer: dict[str, object] | None
    effective_scope: dict[str, object] | None
    as_of: datetime | None
    source_versions: dict[str, str] | None
    usage: dict[str, object] | None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    citations: tuple[StoredCitation, ...] = ()


@dataclass(frozen=True, slots=True)
class Message:
    """One user question or assistant answer."""

    message_id: UUID
    run_id: UUID
    role: str
    sequence: int
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunWork:
    """What a worker needs to execute one run (owner already verified)."""

    run_id: UUID
    conversation_id: UUID
    owner_id: UUID
    question: str
    scope: dict[str, object]
    previous_questions: tuple[str, ...]
    previous_message_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunCompletion:
    """Validated result written atomically with the assistant message."""

    outcome: RunOutcome
    abstention_reason: str | None
    answer: dict[str, object]
    assistant_text: str
    citations: tuple[StoredCitation, ...]
    effective_scope: dict[str, object]
    as_of: datetime
    source_versions: dict[str, str]
    usage: dict[str, object]
    retrieval_query: str
    history_message_ids: tuple[str, ...]


def _filters_from(data: object) -> CatalogFilters:
    if not isinstance(data, dict):
        raise ScopeInvalidError("stored_scope_corrupt")
    values = cast("dict[str, object]", data)

    def strings(key: str) -> tuple[str, ...]:
        raw = cast("object", values.get(key) or [])
        if not isinstance(raw, list):
            raise ScopeInvalidError("stored_scope_corrupt")
        return tuple(str(item) for item in cast("list[object]", raw))

    def moment(key: str) -> datetime | None:
        raw = values.get(key)
        return None if raw is None else datetime.fromisoformat(str(raw))

    q = values.get("q")
    return catalog_filters(
        q=None if q is None else str(q),
        topic_ids=strings("topic_ids"),
        location_ids=strings("location_ids"),
        source_ids=tuple(UUID(s) for s in strings("source_ids")),
        published_after=moment("published_after"),
        published_before=moment("published_before"),
        include_descendants=bool(values.get("include_descendants", True)),
    )


def scope_from_canonical(data: dict[str, object]) -> ConversationScope:
    """Inverse of ``ConversationScope.canonical`` (stored definitions)."""
    try:
        scope_type = ScopeType(str(data.get("type")))
        raw_filters = data.get("filters")
        filters = None if raw_filters is None else _filters_from(raw_filters)
        raw_articles = cast("object", data.get("articles") or [])
        if not isinstance(raw_articles, list):
            raise ScopeInvalidError("stored_scope_corrupt")
        articles = tuple(
            ArticlePin(
                UUID(str(item["article_id"])),
                None
                if item.get("article_version_id") is None
                else str(item["article_version_id"]),
            )
            for item in cast("list[dict[str, object]]", raw_articles)
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ScopeInvalidError):
            raise
        raise ScopeInvalidError("stored_scope_corrupt") from None
    return conversation_scope(scope_type, filters, articles)
