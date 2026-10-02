"""Article persistence capability independent of graph and database frameworks."""

from typing import Protocol

from eventlens_rag.domain.article_document import ArticleDocument


class ArticlePersistence(Protocol):
    """Persist article and lexical visibility before optional indexing."""

    def persist_article(self, document: ArticleDocument) -> None:
        """Commit immutable article revision and lexical visibility."""
        ...
