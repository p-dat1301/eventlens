"""Topic, location and source listings through ``ArticleCatalogPort``."""

from dataclasses import dataclass

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.shared.contracts.catalog import LocationListV1, SourceListV1, TopicListV1


@dataclass(frozen=True, slots=True)
class BrowseTaxonomy:
    """Reference data for filters."""

    catalog: ArticleCatalogPort

    def topics(self) -> TopicListV1:
        """Topic taxonomy."""
        return self.catalog.topics()

    def locations(
        self, parent_id: str | None = None, q: str | None = None, limit: int = 100
    ) -> LocationListV1:
        """Gazetteer units (children of ``parent_id`` / alias prefix ``q``)."""
        return self.catalog.locations(parent_id, q, limit)

    def sources(self) -> SourceListV1:
        """Publishers."""
        return self.catalog.sources()
