"""Catalog queries (no LLM, no LangGraph): list, detail, summary, facets.

Validation, sort defaults and cursors live here; the store implements the
filter semantics. Storage failures propagate as ``CatalogStorageError``.
"""

# ruff: noqa: EM101

from dataclasses import dataclass
from uuid import UUID

from backend.rag.application.ports.catalog import ArticleCatalogStore
from backend.rag.domain.catalog import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    ArticleDetail,
    ArticlePage,
    ArticleSummaryView,
    ArticleVersionStaleError,
    CatalogFacets,
    CatalogFilters,
    CatalogRequestError,
    CatalogSort,
    CursorCodec,
    CursorPosition,
    LocationEntry,
    SourceEntry,
    TopicEntry,
)

MAX_LOCATION_RESULTS = 100


def resolve_sort(sort: str | None, filters: CatalogFilters) -> CatalogSort:
    """Default ``relevance`` with a query, else ``latest``."""
    if sort is None:
        return CatalogSort.RELEVANCE if filters.q else CatalogSort.LATEST
    try:
        chosen = CatalogSort(sort)
    except ValueError:
        raise CatalogRequestError("invalid_sort") from None
    if chosen is CatalogSort.RELEVANCE and not filters.q:
        raise CatalogRequestError("relevance_requires_query")
    return chosen


@dataclass(frozen=True, slots=True)
class CatalogQueries:
    """Read use cases over ``ArticleCatalogStore``."""

    store: ArticleCatalogStore
    cursors: CursorCodec

    def list_articles(
        self,
        filters: CatalogFilters,
        *,
        sort: str | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> ArticlePage:
        """One keyset page of current articles."""
        if not 1 <= limit <= MAX_LIMIT:
            raise CatalogRequestError("invalid_limit")
        order = resolve_sort(sort, filters)
        after = None if cursor is None else self.cursors.decode(cursor, filters, order)
        found, as_of = self.store.list_page(filters, order, after, limit + 1)
        items = found[:limit]
        has_more = len(found) > limit
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = self.cursors.encode(
                filters,
                order,
                CursorPosition(last.card.published_at, last.card.article_id, last.rank),
            )
        return ArticlePage(
            items=tuple(row.card for row in items),
            next_cursor=next_cursor,
            has_more=has_more,
            as_of=as_of,
            search_mode="lexical" if filters.q else "none",
        )

    def article(
        self, article_id: UUID, expected_version: str | None = None
    ) -> ArticleDetail:
        """Current revision; an outdated ``expected_version`` is an error."""
        detail = self.store.article(article_id)
        current = detail.card.article_version_id
        if expected_version is not None and expected_version != current:
            raise ArticleVersionStaleError(current)
        return detail

    def summary(self, article_id: UUID) -> ArticleSummaryView:
        """Stored summary state; never schedules or calls a model."""
        return self.store.summary(article_id)

    def facets(self, filters: CatalogFilters) -> CatalogFacets:
        """Counts within every selected filter."""
        return self.store.facets(filters)

    def topics(self) -> tuple[TopicEntry, ...]:
        """Topic taxonomy."""
        return self.store.topics()

    def locations(
        self,
        parent_id: str | None = None,
        query: str | None = None,
        limit: int = MAX_LOCATION_RESULTS,
    ) -> tuple[LocationEntry, ...]:
        """Gazetteer lookup."""
        if not 1 <= limit <= MAX_LOCATION_RESULTS:
            raise CatalogRequestError("invalid_limit")
        if query is not None and len(query) > MAX_QUERY_CHARS:
            raise CatalogRequestError("query_too_long")
        return self.store.locations(parent_id, query, limit)

    def sources(self) -> tuple[SourceEntry, ...]:
        """Publishers."""
        return self.store.sources()
