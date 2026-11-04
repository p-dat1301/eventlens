"""Article catalog ports (read models over current revisions).

Adapters raise ``CatalogStorageError`` when storage is unavailable or a
statement fails, never an empty result, and ``CatalogRequestError`` for
unknown topic/location IDs.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.rag.domain.catalog import (
    ArticleDetail,
    ArticleSummaryView,
    CatalogFacets,
    CatalogFilters,
    CatalogRow,
    CatalogSort,
    CursorPosition,
    LocationEntry,
    SourceEntry,
    TopicEntry,
)
from backend.rag.domain.taxonomy import DerivedLabels
from backend.shared.contracts import ArticleEnrichment


@dataclass(frozen=True, slots=True)
class ScopeSnapshot:
    """Current revisions matching a filter definition at ``as_of``."""

    versions: dict[UUID, str]
    total: int
    as_of: datetime


class ArticleCatalogStore(Protocol):
    """One implementation of the catalog filter semantics."""

    def list_page(
        self,
        filters: CatalogFilters,
        sort: CatalogSort,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[tuple[CatalogRow, ...], datetime]:
        """Up to ``limit`` rows after ``after`` plus the read time."""
        ...

    def article(self, article_id: UUID) -> ArticleDetail:
        """Current revision or ``ArticleNotFoundError``/``ArticleGoneError``."""
        ...

    def summary(self, article_id: UUID) -> ArticleSummaryView:
        """Summary state of the current revision (no model call)."""
        ...

    def facets(self, filters: CatalogFilters) -> CatalogFacets:
        """Distinct current-article counts under every filter."""
        ...

    def topics(self) -> tuple[TopicEntry, ...]:
        """Taxonomy topics in display order."""
        ...

    def locations(
        self, parent_id: str | None, query: str | None, limit: int
    ) -> tuple[LocationEntry, ...]:
        """Gazetteer units, optionally children of ``parent_id`` / alias prefix."""
        ...

    def sources(self) -> tuple[SourceEntry, ...]:
        """Publishers of current articles."""
        ...

    def resolve_scope(self, filters: CatalogFilters, cap: int) -> ScopeSnapshot:
        """Every matching current revision (at most ``cap`` + 1 entries)."""
        ...

    def current_versions(self, article_ids: tuple[UUID, ...]) -> dict[UUID, str]:
        """Current non-tombstoned revision of each known article."""
        ...


@dataclass(frozen=True, slots=True)
class LabelSource:
    """Current revision and its single valid READY publication (if any)."""

    article_id: UUID
    content_hash: str
    publication_id: int | None
    metadata: ArticleEnrichment | None
    metadata_invalid: bool


class ArticleLabelStore(Protocol):
    """Fenced label projection of enrichment publications."""

    def pending(self, limit: int, trusted: frozenset[str]) -> tuple[LabelSource, ...]:
        """Revisions never synced, re-published, or whose trust decision changed.

        A recorded ``untrusted_model`` outcome is re-processed once its model
        is trusted; an ``applied`` one once its model is no longer trusted
        (the sync then removes the model labels; manual labels stay).
        """
        ...

    def alias_index(self) -> dict[str, str]:
        """Gazetteer alias key -> location ID."""
        ...

    def apply(
        self,
        source: LabelSource,
        labels: DerivedLabels | None,
        outcome: str,
    ) -> bool:
        """Replace model labels of the revision if it is still current."""
        ...
