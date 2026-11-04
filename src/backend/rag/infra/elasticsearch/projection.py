"""Projection document <-> Elasticsearch ``_source`` (no I/O)."""

from datetime import datetime
from typing import cast
from uuid import UUID

from backend.rag.domain.search_index import SearchBranch, SearchChunkDocument, SearchHit

SOURCE_FIELDS = (
    "article_id",
    "revision_id",
    "chunk_id",
    "content_hash",
    "chunk_version",
    "chunk_index",
    "char_start",
    "char_end",
    "text",
    "published_at",
    "source_domain",
    "generation_id",
)


def to_source(document: SearchChunkDocument) -> dict[str, object]:
    """Indexable source; the vector is omitted for lexical-only generations."""
    source: dict[str, object] = {
        "doc_kind": "chunk",
        "projection_version": document.projection_version,
        "article_id": str(document.article_id),
        "revision_id": str(document.revision_id),
        "chunk_id": str(document.chunk_id),
        "content_hash": document.content_hash,
        "chunk_version": document.chunk_version,
        "chunk_index": document.chunk_index,
        "char_start": document.char_start,
        "char_end": document.char_end,
        "paragraph_start": document.paragraph_start,
        "paragraph_end": document.paragraph_end,
        "title": document.title,
        "sapo": document.sapo,
        "text": document.text,
        "source_name": document.source_name,
        "source_domain": document.source_domain,
        "published_at": (
            None if document.published_at is None else document.published_at.isoformat()
        ),
        "topics": list(document.topics),
        "entity_ids": list(document.entity_ids),
        "access_scopes": list(document.access_scopes),
        "embedding_model": document.embedding_model,
        "embedding_identity": document.embedding_identity,
        "embedding_dimensions": document.embedding_dimensions,
        "generation_id": document.generation_id,
        "is_current": document.is_current,
        "tombstoned": document.tombstoned,
    }
    if document.embedding is not None:
        source["embedding"] = list(document.embedding)
    return source


def _text(source: dict[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        msg = f"search hit field {key} missing"
        raise TypeError(msg)
    return value


def _int(source: dict[str, object], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"search hit field {key} missing"
        raise TypeError(msg)
    return value


def from_hit(raw: dict[str, object], branch: SearchBranch, rank: int) -> SearchHit:
    """Typed hit from one ``hits.hits[]`` entry."""
    source = cast("dict[str, object]", raw.get("_source", {}))
    published = source.get("published_at")
    score = raw.get("_score")
    return SearchHit(
        chunk_id=UUID(_text(source, "chunk_id")),
        article_id=UUID(_text(source, "article_id")),
        revision_id=UUID(_text(source, "revision_id")),
        content_hash=_text(source, "content_hash"),
        chunk_version=_text(source, "chunk_version"),
        chunk_index=_int(source, "chunk_index"),
        char_start=_int(source, "char_start"),
        char_end=_int(source, "char_end"),
        text=_text(source, "text"),
        published_at=(
            datetime.fromisoformat(published) if isinstance(published, str) else None
        ),
        source_domain=_text(source, "source_domain"),
        generation_id=_int(source, "generation_id"),
        branch=branch,
        rank=rank,
        score=float(score) if isinstance(score, int | float) else 0.0,
    )
