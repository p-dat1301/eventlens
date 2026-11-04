"""Catalog stand-in when rag-api has no database: every read is ``503``.

Never an empty page: ``catalog_unavailable`` tells the caller storage is not
configured instead of pretending there are no articles.
"""

from datetime import datetime
from uuid import UUID

from backend.rag.application.ports.catalog import ScopeSnapshot
from backend.rag.application.use_cases.catalog import CatalogQueries
from backend.rag.domain.catalog import (
    ArticleDetail,
    ArticleSummaryView,
    CatalogFacets,
    CatalogFilters,
    CatalogRow,
    CatalogSort,
    CatalogStorageError,
    CursorCodec,
    CursorPosition,
    LocationEntry,
    SourceEntry,
    TopicEntry,
)


class _NoCatalog:
    def list_page(
        self,
        filters: CatalogFilters,
        sort: CatalogSort,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[tuple[CatalogRow, ...], datetime]:
        del filters, sort, after, limit
        raise CatalogStorageError

    def article(self, article_id: UUID) -> ArticleDetail:
        del article_id
        raise CatalogStorageError

    def summary(self, article_id: UUID) -> ArticleSummaryView:
        del article_id
        raise CatalogStorageError

    def facets(self, filters: CatalogFilters) -> CatalogFacets:
        del filters
        raise CatalogStorageError

    def topics(self) -> tuple[TopicEntry, ...]:
        raise CatalogStorageError

    def locations(
        self, parent_id: str | None, query: str | None, limit: int
    ) -> tuple[LocationEntry, ...]:
        del parent_id, query, limit
        raise CatalogStorageError

    def sources(self) -> tuple[SourceEntry, ...]:
        raise CatalogStorageError

    def resolve_scope(self, filters: CatalogFilters, cap: int) -> ScopeSnapshot:
        del filters, cap
        raise CatalogStorageError

    def current_versions(self, article_ids: tuple[UUID, ...]) -> dict[UUID, str]:
        del article_ids
        raise CatalogStorageError


def unavailable_catalog() -> CatalogQueries:
    """Catalog whose every read raises ``CatalogStorageError``."""
    return CatalogQueries(
        _NoCatalog(), CursorCodec(b"unavailable-catalog-no-cursors!!")
    )
