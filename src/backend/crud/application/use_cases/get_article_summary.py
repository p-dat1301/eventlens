"""Stored article summary state through ``ArticleCatalogPort``."""

from dataclasses import dataclass
from uuid import UUID

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.shared.contracts.catalog import ArticleSummaryV1


@dataclass(frozen=True, slots=True)
class GetArticleSummary:
    """Reads only; never triggers a model call."""

    catalog: ArticleCatalogPort

    def execute(self, article_id: UUID) -> ArticleSummaryV1:
        """Summary or its pending/failed/unavailable state."""
        return self.catalog.get_summary(article_id)
