"""Deterministic JSON Schema rendering for checked-in contracts."""

import json
from collections.abc import Callable

from pydantic import BaseModel

from eventlens_contracts import (
    Article,
    ArticleBatchManifest,
    ArticleDocument,
    ArticleEnrichment,
    ArticleMedia,
    ArticleRejected,
    BriefingAnswer,
    BriefingQuery,
    Event,
)


def _schema_json(model: type[BaseModel]) -> str:
    return f"{json.dumps(model.model_json_schema(), indent=2, sort_keys=True)}\n"


def article_schema_json() -> str:
    """Render Article v1 JSON Schema."""
    return _schema_json(Article)


def article_media_schema_json() -> str:
    """Render ArticleMedia v1 JSON Schema."""
    return _schema_json(ArticleMedia)


def article_rejected_schema_json() -> str:
    """Render ArticleRejected v1 JSON Schema."""
    return _schema_json(ArticleRejected)


def article_batch_manifest_schema_json() -> str:
    """Render ArticleBatchManifest v1 JSON Schema."""
    return _schema_json(ArticleBatchManifest)


def event_schema_json() -> str:
    """Render Event v1 JSON Schema."""
    return _schema_json(Event)


def article_document_schema_json() -> str:
    """Render ArticleDocument v1 JSON Schema."""
    return _schema_json(ArticleDocument)


def article_enrichment_schema_json() -> str:
    """Render ArticleEnrichment v1 JSON Schema."""
    return _schema_json(ArticleEnrichment)


def briefing_query_schema_json() -> str:
    """Render BriefingQuery v1 JSON Schema."""
    return _schema_json(BriefingQuery)


def briefing_answer_schema_json() -> str:
    """Render BriefingAnswer v1 JSON Schema."""
    return _schema_json(BriefingAnswer)


SCHEMA_RENDERERS: tuple[tuple[str, Callable[[], str]], ...] = (
    ("article.v1.schema.json", article_schema_json),
    ("article_media.v1.schema.json", article_media_schema_json),
    ("article_rejected.v1.schema.json", article_rejected_schema_json),
    ("article_batch_manifest.v1.schema.json", article_batch_manifest_schema_json),
    ("event.v1.schema.json", event_schema_json),
    ("article_document.v1.schema.json", article_document_schema_json),
    ("article_enrichment.v1.schema.json", article_enrichment_schema_json),
    ("briefing_query.v1.schema.json", briefing_query_schema_json),
    ("briefing_answer.v1.schema.json", briefing_answer_schema_json),
)
