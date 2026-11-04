"""Persist handoff revisions together with their upstream provenance."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.handoff import SourceProvenance


class ProvenanceConflictError(RuntimeError):
    """An upstream version is already bound to a different RAG revision."""

    code: ClassVar[str] = "provenance_conflict"

    def __init__(self, article_version_id: UUID) -> None:
        """Name the conflicting upstream version; no content is included."""
        super().__init__(f"article_version_id {article_version_id} already bound")


@dataclass(frozen=True, slots=True)
class PersistOutcome:
    """What one idempotent persist changed."""

    revision_inserted: bool
    provenance_inserted: bool
    current: bool


class SourceVersionStore(Protocol):
    """Revision, lexical pointer (and its outbox event) plus provenance."""

    def persist_batch(
        self, items: Sequence[tuple[ArticleDocument, SourceProvenance]]
    ) -> tuple[PersistOutcome, ...]:
        """Persist every pair in ONE transaction; any conflict writes nothing."""
        ...

    def provenance_for(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[SourceProvenance, ...]:
        """Every recorded upstream version of these articles (one read)."""
        ...
