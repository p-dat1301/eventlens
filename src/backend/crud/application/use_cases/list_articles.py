"""List current articles (feed/search) through ``ArticleCatalogPort``."""

from dataclasses import dataclass

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.crud.domain.catalog import ArticleListParams
from backend.shared.contracts.catalog import ArticleListV1


@dataclass(frozen=True, slots=True)
class ListArticles:
    """Feed page; storage failures propagate as typed port errors."""

    catalog: ArticleCatalogPort

    def execute(self, params: ArticleListParams) -> ArticleListV1:
        """Forward the validated parameters."""
        return self.catalog.list_articles(params)
