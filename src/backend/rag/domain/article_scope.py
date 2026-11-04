"""Server-resolved article restriction applied before retrieval.

Built by the conversation run from the stored scope definition (catalog
semantics, current revisions) and never from client-supplied evidence. It is
intersected into ``RetrievalScope.allowed_article_ids`` before the
PostgreSQL/Elasticsearch branches run, and the final current-revision check
drops any article outside it or whose revision differs from the resolved one.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ArticleRestriction:
    """Allowed article IDs mapped to the revision each must still have."""

    versions: Mapping[UUID, str]

    @property
    def article_ids(self) -> frozenset[UUID]:
        """Allowed article IDs."""
        return frozenset(self.versions)

    def admits(self, article_id: UUID, content_hash: str) -> bool:
        """In scope and still at the resolved revision."""
        return self.versions.get(article_id) == content_hash
