"""Deterministic JSON Schema rendering from contract models."""

import json
from collections.abc import Callable

from pydantic import BaseModel

from eventlens_contracts.article import Article
from eventlens_contracts.enrichment import ArticleEnrichment
from eventlens_contracts.event import Event
from eventlens_contracts.manifest import ArticleBatchManifest
from eventlens_contracts.media import ArticleMedia
from eventlens_contracts.rejected import ArticleRejected


def schema_json(model: type[BaseModel]) -> str:
    """Render deterministic JSON Schema for a model."""
    return f"{json.dumps(model.model_json_schema(), indent=2, sort_keys=True)}\n"


def article_schema_json() -> str:
    """Render Article v1 JSON Schema."""
    return schema_json(Article)


def article_media_schema_json() -> str:
    """Render ArticleMedia v1 JSON Schema."""
    return schema_json(ArticleMedia)


def article_rejected_schema_json() -> str:
    """Render ArticleRejected v1 JSON Schema."""
    return schema_json(ArticleRejected)


def article_batch_manifest_schema_json() -> str:
    """Render ArticleBatchManifest v1 JSON Schema."""
    return schema_json(ArticleBatchManifest)


def event_schema_json() -> str:
    """Render Event v1 JSON Schema."""
    return schema_json(Event)


def article_enrichment_schema_json() -> str:
    """Render ArticleEnrichment v1 JSON Schema."""
    return schema_json(ArticleEnrichment)


SCHEMA_RENDERERS: tuple[tuple[str, Callable[[], str]], ...] = (
    ("article.v1.schema.json", article_schema_json),
    ("article_media.v1.schema.json", article_media_schema_json),
    ("article_rejected.v1.schema.json", article_rejected_schema_json),
    ("article_batch_manifest.v1.schema.json", article_batch_manifest_schema_json),
    ("event.v1.schema.json", event_schema_json),
    ("article_enrichment.v1.schema.json", article_enrichment_schema_json),
)
