"""Article detail through ``ArticleCatalogPort``."""

from dataclasses import dataclass
from uuid import UUID

from backend.crud.application.ports.article_catalog import ArticleCatalogPort
from backend.shared.contracts.catalog import ArticleDetailV1


@dataclass(frozen=True, slots=True)
class GetArticle:
    """Current revision; a stale ``article_version_id`` is rejected upstream."""

    catalog: ArticleCatalogPort

    def execute(
        self, article_id: UUID, article_version_id: str | None = None
    ) -> ArticleDetailV1:
        """Detail of one current article."""
        return self.catalog.get_article(article_id, article_version_id)
