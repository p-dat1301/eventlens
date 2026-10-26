"""Opaque source references and the read-only source viewer use case (D37).

A reference is a random token mapped server-side to (caller, article, revision
hash, span). It carries no lineage and cannot be forged or decoded. Every view
re-reads the current revision, so a corrected, tombstoned or revoked source is
never served from a cache. Framework-free: no FastAPI, SQL or provider code.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import ClassVar

from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.source_references import SourceReferenceRegistry
from backend.rag.application.ports.synthesis import CurrentArticleReader
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.shared.contracts.evidence import EvidenceSpan

MAX_REFERENCE_LENGTH = 64


class SourceError(RuntimeError):
    """Base typed source-viewer failure (fixed messages, no content)."""

    code: ClassVar[str] = "source_failed"


class SourceNotFoundError(SourceError):
    """Unknown, expired or foreign reference (existence is never revealed)."""

    code: ClassVar[str] = "source_not_found"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("source reference not found")


class SourceAccessDeniedError(SourceError):
    """Caller policy no longer allows source access."""

    code: ClassVar[str] = "source_access_denied"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("source access denied")


class SourceStaleError(SourceError):
    """Article revision, tombstone or span no longer matches the citation."""

    code: ClassVar[str] = "source_stale"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("cited source is no longer current")


@dataclass(frozen=True, slots=True)
class CitedSpanView:
    """Authorized cited span of the current revision."""

    start_offset: int
    end_offset: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class SourceView:
    """Allowlisted source fields for the internal viewer."""

    title: str
    source_name: str
    source_domain: str
    source_url: str
    source_published_at: datetime | None
    normalized_content: str
    span: CitedSpanView


def _fingerprint(text: str) -> str:
    return f"sha256:{sha256(text.encode()).hexdigest()}"


def _span_still_current(document: ArticleDocument, span: EvidenceSpan) -> bool:
    if document.content_hash != span.content_hash:
        return False
    if not 0 <= span.start_offset < span.end_offset <= len(document.normalized_content):
        return False
    excerpt = document.normalized_content[span.start_offset : span.end_offset]
    return excerpt == span.excerpt and _fingerprint(excerpt) == span.span_fingerprint


@dataclass(frozen=True, slots=True)
class InspectSource:
    """Resolve one opaque reference against the current article revision."""

    references: SourceReferenceRegistry
    sources: CurrentArticleReader
    access: AccessPolicy

    def execute(self, token: str, caller: CallerScope) -> SourceView:
        """Return the cited source or raise a typed ``SourceError``.

        ``RetrievalStorageError`` from the batched read propagates unchanged.
        """
        if not self.access.allows(caller):
            raise SourceAccessDeniedError
        reference = (
            self.references.resolve(token)
            if 0 < len(token) <= MAX_REFERENCE_LENGTH
            else None
        )
        if (
            reference is None
            or reference.principal_id != caller.principal_id
            or reference.scope_id != caller.scope_id
        ):
            raise SourceNotFoundError
        span = reference.span
        current = self.sources.current_articles((span.article_id,))
        if not self.access.allows(caller):  # revoked during the read
            raise SourceAccessDeniedError
        document = next(
            (s.document for s in current if s.document.article_id == span.article_id),
            None,
        )
        if (
            document is None
            or document.visibility != "internal"
            or not _span_still_current(document, span)
        ):
            raise SourceStaleError
        return SourceView(
            title=document.title,
            source_name=document.source_name,
            source_domain=document.source_domain,
            source_url=str(document.canonical_url),
            source_published_at=document.source_published_at,
            normalized_content=document.normalized_content,
            span=CitedSpanView(span.start_offset, span.end_offset, span.excerpt),
        )
