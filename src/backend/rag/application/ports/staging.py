"""Vector generation staging capability independent of database frameworks."""

from typing import Protocol

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.staging import StageRequest


class GenerationStaging(Protocol):
    """Find reusable complete generations and stage new inactive ones."""

    def find_complete(
        self, article: ArticleDocument, identity: EmbeddingIdentity
    ) -> int | None:
        """Return a complete generation for this exact embedding key, if any."""
        ...

    def stage(self, request: StageRequest) -> int:
        """Stage one complete inactive generation and return its identifier."""
        ...
