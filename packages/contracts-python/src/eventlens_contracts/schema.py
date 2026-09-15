"""Deterministic JSON Schema rendering for checked-in contracts."""

import json
from collections.abc import Callable

from pydantic import BaseModel

from eventlens_contracts import (
    Article,
    ArticleBatchManifest,
    ArticleMedia,
    ArticleRejected,
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


SCHEMA_RENDERERS: tuple[tuple[str, Callable[[], str]], ...] = (
    ("article.v1.schema.json", article_schema_json),
    ("article_media.v1.schema.json", article_media_schema_json),
    ("article_rejected.v1.schema.json", article_rejected_schema_json),
    ("article_batch_manifest.v1.schema.json", article_batch_manifest_schema_json),
    ("event.v1.schema.json", event_schema_json),
)
