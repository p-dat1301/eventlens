"""Facet counts through ``ArticleCatalogPort``."""

from dataclasses import dataclass

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.crud.domain.catalog import CatalogFilterParams
from backend.shared.contracts.catalog import ArticleFacetsV1


@dataclass(frozen=True, slots=True)
class ListFacets:
    """Counts with every selected filter applied."""

    catalog: ArticleCatalogPort

    def execute(self, filters: CatalogFilterParams) -> ArticleFacetsV1:
        """Facets of the filtered set."""
        return self.catalog.facets(filters)
