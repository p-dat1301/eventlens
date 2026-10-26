"""Render RAG-owned JSON schemas with shared deterministic formatting."""

from collections.abc import Callable

from backend.rag.domain.answer import BriefingAnswer
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery
from backend.shared.contracts.schema import schema_json


def article_document_schema_json() -> str:
    """Render ArticleDocument v1 JSON Schema."""
    return schema_json(ArticleDocument)


def briefing_query_schema_json() -> str:
    """Render BriefingQuery v1 JSON Schema."""
    return schema_json(BriefingQuery)


def briefing_answer_schema_json() -> str:
    """Render BriefingAnswer v1 JSON Schema."""
    return schema_json(BriefingAnswer)


SCHEMA_RENDERERS: tuple[tuple[str, Callable[[], str]], ...] = (
    ("article_document.v1.schema.json", article_document_schema_json),
    ("briefing_query.v1.schema.json", briefing_query_schema_json),
    ("briefing_answer.v1.schema.json", briefing_answer_schema_json),
)
