"""``ArticleCatalogPort``: the web API's view of the RAG-owned catalog.

crud-api owns no article tables; an adapter reads the RAG internal catalog
API. Adapters raise:

* ``CatalogUpstreamError`` for a typed rejection by the catalog (bad filter,
  unknown/removed article, stale version) that the web client must see;
* ``CatalogUnavailableError`` when the catalog cannot answer (never an empty
  page);
* ``CatalogContractError`` when the response does not match ``catalog.v1``.
"""

from typing import Protocol
from uuid import UUID

from backend.crud.domain.catalog import ArticleListParams, CatalogFilterParams
from backend.shared.contracts.catalog import (
    ArticleDetailV1,
    ArticleFacetsV1,
    ArticleListV1,
    ArticleSummaryV1,
    LocationListV1,
    SourceListV1,
    TopicListV1,
)


class CatalogUpstreamError(Exception):
    """Typed client-visible rejection (status/code/reason from the catalog)."""

    def __init__(self, status: int, code: str, reason: str | None) -> None:
        """Keep the stable fields only."""
        super().__init__(code)
        self.status: int = status
        self.code: str = code
        self.reason: str | None = reason


class CatalogUnavailableError(Exception):
    """Catalog unreachable, timed out, misconfigured or circuit open."""

    code: str = "catalog_unavailable"

    def __init__(self, reason: str) -> None:
        """Enum reason only (never URLs, tokens or bodies)."""
        super().__init__(reason)
        self.reason: str = reason


class CatalogContractError(Exception):
    """The catalog answered something that is not ``catalog.v1``."""

    code: str = "catalog_contract_mismatch"


class ArticleCatalogPort(Protocol):
    """Read-only catalog operations used by the web API."""

    def list_articles(self, params: ArticleListParams) -> ArticleListV1:
        """One page of current articles."""
        ...

    def get_article(
        self, article_id: UUID, article_version_id: str | None
    ) -> ArticleDetailV1:
        """Current revision detail."""
        ...

    def get_summary(self, article_id: UUID) -> ArticleSummaryV1:
        """Stored summary state."""
        ...

    def facets(self, filters: CatalogFilterParams) -> ArticleFacetsV1:
        """Facet counts under all filters."""
        ...

    def topics(self) -> TopicListV1:
        """Topic taxonomy."""
        ...

    def locations(
        self, parent_id: str | None, q: str | None, limit: int
    ) -> LocationListV1:
        """Gazetteer lookup."""
        ...

    def sources(self) -> SourceListV1:
        """Publishers."""
        ...
