"""Scripted persistence port for application tests, never DB acceptance."""

from dataclasses import dataclass, field

from backend.rag.domain.article_document import ArticleDocument


@dataclass(frozen=True, slots=True)
class ScriptedPersistence:
    """Record calls in an invocation-owned script fixture."""

    calls: list[ArticleDocument] = field(default_factory=list)
    failure: OSError | None = None

    def persist_article(self, document: ArticleDocument) -> None:
        """Record successful persistence or raise configured failure."""
        if self.failure is not None:
            raise self.failure
        self.calls.append(document)
