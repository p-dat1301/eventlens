"""Versioned index settings and mapping (``search-mapping.v1``).

Text fields use ``vi_text``: the standard tokenizer (Vietnamese is written in
space-separated syllables), lowercase, then ASCII folding with the original
token preserved, so ``Hà Nội`` matches both ``hà nội`` and ``ha noi``. No
dictionary word segmentation is done; its quality is measured, not assumed
(``docs/search-evaluation.md``). Title is boosted above sapo and body at
query time (``bm25.py``). Identifiers are ``keyword``; the embedding is a
cosine ``dense_vector`` with an explicit float HNSW index. ``doc_kind`` is
``chunk`` for searchable chunks and ``fence`` for the per-article write fence
(external version = projection version); searches and counts filter chunks.
"""

import json
from hashlib import sha256
from typing import Final

MAPPING_VERSION: Final = "search-mapping.v2"
ANALYZER: Final = "vi_text"

SETTINGS: Final[dict[str, object]] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "analysis": {
        "filter": {
            "vi_fold": {"type": "asciifolding", "preserve_original": True},
        },
        "analyzer": {
            ANALYZER: {
                "type": "custom",
                "tokenizer": "standard",
                "filter": ["lowercase", "vi_fold"],
            }
        },
    },
}


def mappings(dimensions: int | None) -> dict[str, object]:
    """Strict mapping; ``dimensions=None`` builds a lexical-only index."""
    text = {"type": "text", "analyzer": ANALYZER}
    properties: dict[str, object] = {
        "doc_kind": {"type": "keyword"},
        "projection_version": {"type": "long"},
        "article_id": {"type": "keyword"},
        "revision_id": {"type": "keyword"},
        "chunk_id": {"type": "keyword"},
        "content_hash": {"type": "keyword"},
        "chunk_version": {"type": "keyword"},
        "chunk_index": {"type": "integer"},
        "char_start": {"type": "integer"},
        "char_end": {"type": "integer"},
        "paragraph_start": {"type": "integer"},
        "paragraph_end": {"type": "integer"},
        "title": {**text, "fields": {"raw": {"type": "keyword", "ignore_above": 512}}},
        "sapo": text,
        "text": text,
        "source_name": {"type": "keyword"},
        "source_domain": {"type": "keyword"},
        "published_at": {"type": "date"},
        "topics": {"type": "keyword"},
        "entity_ids": {"type": "keyword"},
        "access_scopes": {"type": "keyword"},
        "embedding_model": {"type": "keyword"},
        "embedding_identity": {"type": "keyword"},
        "embedding_dimensions": {"type": "integer"},
        "generation_id": {"type": "long"},
        "is_current": {"type": "boolean"},
        "tombstoned": {"type": "boolean"},
    }
    if dimensions is not None:
        properties["embedding"] = {
            "type": "dense_vector",
            "dims": dimensions,
            "index": True,
            "similarity": "cosine",
            "index_options": {"type": "hnsw", "m": 16, "ef_construction": 100},
        }
    return {"dynamic": "strict", "properties": properties}


def index_body(dimensions: int | None) -> dict[str, object]:
    """Create-index request body."""
    return {
        "settings": SETTINGS,
        "mappings": {
            **mappings(dimensions),
            "_meta": {"mapping_version": MAPPING_VERSION},
        },
    }


def mapping_digest(dimensions: int | None) -> str:
    """SHA-256 of the canonical create body (recorded in reports)."""
    body = json.dumps(index_body(dimensions), sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(body.encode()).hexdigest()}"
