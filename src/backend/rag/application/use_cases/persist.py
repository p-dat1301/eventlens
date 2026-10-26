"""Authorized persist-first application boundary."""

from dataclasses import dataclass

from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.persistence import ArticlePersistence
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument


class AccessDeniedError(PermissionError):
    """Caller no longer satisfies fixture access policy."""


@dataclass(frozen=True, slots=True)
class PersistArticle:
    """Authorize current caller before invoking injected persistence."""

    repository: ArticlePersistence
    access: AccessPolicy

    def execute(self, article: ArticleDocument, caller: CallerScope) -> None:
        """Commit searchable document without enrichment/provider work."""
        if not self.access.allows(caller):
            raise AccessDeniedError(caller.principal_id)
        self.repository.persist_article(article)
