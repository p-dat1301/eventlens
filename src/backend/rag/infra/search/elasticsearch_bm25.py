"""Elasticsearch BM25 chunk index over plain HTTP (no SDK).

Only indices named ``{prefix}-{analyzer_id}`` are created or deleted. Scope
filters (source domains, publication window, allowed article IDs) run as a
non-scoring ``bool.filter``, so they constrain candidates before ranking depth
is cut. Missing ``published_at`` never satisfies a range, which matches the
PostgreSQL rule "NULL excluded once any bound is set".
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, ClassVar, Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.retrieval import ChunkRef

if TYPE_CHECKING:
    from http.client import HTTPResponse

from backend.rag.domain.search_index import (
    IndexedChunk,
    LexicalIndexIdentity,
    ScoredChunk,
)

INDEX_PREFIX: Final = "eventlens-rag-chunks"
BULK_BATCH: Final = 500
BM25_K1: Final = 1.2
BM25_B: Final = 0.75

# Versioned analyzers. Vietnamese is written in space-separated syllables;
# none of these performs dictionary word segmentation (recorded as a limit).
ANALYZERS: Final[Mapping[str, Mapping[str, object]]] = {
    "vi-standard.v1": {
        "analyzer": {
            "chunk_text": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": ["lowercase"],
            }
        }
    },
    "vi-standard-fold.v1": {
        "filter": {"fold": {"type": "asciifolding", "preserve_original": True}},
        "analyzer": {
            "chunk_text": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": ["lowercase", "fold"],
            }
        },
    },
    "vi-syllable-bigram.v1": {
        "filter": {
            "bigram": {
                "type": "shingle",
                "min_shingle_size": 2,
                "max_shingle_size": 2,
                "output_unigrams": True,
            }
        },
        "analyzer": {
            "chunk_text": {
                "type": "custom",
                "tokenizer": "standard",
                "filter": ["lowercase", "bigram"],
            }
        },
    },
}


class SearchEngineError(RuntimeError):
    """Elasticsearch unreachable or refused a request (no payload echoed)."""

    code: ClassVar[str] = "search_engine_failed"


def analyzer_digest(analyzer_id: str) -> str:
    """SHA-256 of the canonical analyzer settings."""
    body = json.dumps(ANALYZERS[analyzer_id], sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256(body.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ElasticsearchBm25Index:
    """``ChunkLexicalIndex`` backed by one Elasticsearch index per analyzer."""

    base_url: str
    analyzer_id: str
    chunker_version: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        """Reject unknown analyzers before any request."""
        if self.analyzer_id not in ANALYZERS:
            msg = f"unknown analyzer {self.analyzer_id}"
            raise ValueError(msg)

    @property
    def index_name(self) -> str:
        """Owned index name; nothing outside the prefix is ever touched."""
        return f"{INDEX_PREFIX}-{self.analyzer_id.replace('.', '-')}"

    @property
    def identity(self) -> LexicalIndexIdentity:
        """Engine version is read from the server at call time."""
        info = self._request("GET", "/")
        version = cast("dict[str, object]", info.get("version", {}))
        return LexicalIndexIdentity(
            engine="elasticsearch",
            engine_version=str(version.get("number", "unknown")),
            analyzer_id=self.analyzer_id,
            analyzer_digest=analyzer_digest(self.analyzer_id),
            similarity=f"BM25(k1={BM25_K1},b={BM25_B})",
        )

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        *,
        missing_ok: bool = False,
    ) -> dict[str, object]:
        request = Request(  # noqa: S310 - operator-provided http(s) base URL
            self.base_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={"Content-Type": content_type},
        )
        try:
            with cast(
                "HTTPResponse",
                urlopen(request, timeout=self.timeout_seconds),  # noqa: S310
            ) as response:
                payload = response.read()
        except HTTPError as error:
            if missing_ok and error.code == 404:  # noqa: PLR2004
                return {}
            msg = f"elasticsearch {method} returned HTTP {error.code}"
            raise SearchEngineError(msg) from None
        except (URLError, TimeoutError, OSError):
            msg = f"elasticsearch {method} unreachable"
            raise SearchEngineError(msg) from None
        return cast("dict[str, object]", json.loads(payload)) if payload else {}

    def rebuild(self, chunks: Sequence[IndexedChunk]) -> None:
        """Drop and recreate the owned index, bulk-load chunks, refresh."""
        name = quote(self.index_name)
        _ = self._request("DELETE", f"/{name}", missing_ok=True)
        settings = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "analysis": ANALYZERS[self.analyzer_id],
                "similarity": {"default": {"type": "BM25", "k1": BM25_K1, "b": BM25_B}},
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "text": {"type": "text", "analyzer": "chunk_text"},
                    "article_id": {"type": "keyword"},
                    "content_hash": {"type": "keyword"},
                    "chunker_version": {"type": "keyword"},
                    "chunk_index": {"type": "integer"},
                    "start_offset": {"type": "integer"},
                    "end_offset": {"type": "integer"},
                    "source_domain": {"type": "keyword"},
                    "published_at": {"type": "date"},
                },
            },
        }
        _ = self._request("PUT", f"/{name}", json.dumps(settings).encode())
        for start in range(0, len(chunks), BULK_BATCH):
            lines: list[str] = []
            for item in chunks[start : start + BULK_BATCH]:
                if item.chunk.chunker_version != self.chunker_version:
                    msg = "chunk chunker_version differs from the index"
                    raise ValueError(msg)
                lines.append(json.dumps({"index": {"_id": str(item.chunk.chunk_id)}}))
                lines.append(
                    json.dumps(
                        {
                            "text": item.text,
                            "article_id": str(item.chunk.article_id),
                            "content_hash": item.chunk.content_hash,
                            "chunker_version": item.chunk.chunker_version,
                            "chunk_index": item.chunk.chunk_index,
                            "start_offset": item.chunk.start_offset,
                            "end_offset": item.chunk.end_offset,
                            "source_domain": item.source_domain,
                            "published_at": None
                            if item.source_published_at is None
                            else item.source_published_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
            result = self._request(
                "POST",
                f"/{name}/_bulk",
                ("\n".join(lines) + "\n").encode(),
                "application/x-ndjson",
            )
            if result.get("errors"):
                msg = "elasticsearch bulk load reported item errors"
                raise SearchEngineError(msg)
        _ = self._request("POST", f"/{name}/_refresh")

    def search(
        self, query: str, scope: RetrievalScope, limit: int
    ) -> tuple[ScoredChunk, ...]:
        """BM25 ``match`` (OR of analyzed terms) under the scope filter."""
        filters: list[dict[str, object]] = []
        if scope.source_domains:
            filters.append({"terms": {"source_domain": list(scope.source_domains)}})
        window: dict[str, str] = {}
        if scope.published_after is not None:
            window["gte"] = scope.published_after.isoformat()
        if scope.published_before is not None:
            window["lt"] = scope.published_before.isoformat()
        if window:
            filters.append({"range": {"published_at": window}})
        if scope.allowed_article_ids is not None:
            filters.append(
                {"terms": {"article_id": [str(i) for i in scope.allowed_article_ids]}}
            )
        body = {
            "size": limit,
            "track_total_hits": False,
            "_source": [
                "article_id",
                "content_hash",
                "chunker_version",
                "chunk_index",
                "start_offset",
                "end_offset",
                "published_at",
            ],
            "query": {
                "bool": {"must": [{"match": {"text": query}}], "filter": filters}
            },
            # Deterministic order for equal scores.
            "sort": [{"_score": "desc"}, {"article_id": "asc"}, {"chunk_index": "asc"}],
        }
        result = self._request(
            "POST", f"/{quote(self.index_name)}/_search", json.dumps(body).encode()
        )
        hits = cast("dict[str, object]", result.get("hits", {}))
        return tuple(_hit(item) for item in cast("list[object]", hits.get("hits", [])))

    def size_bytes(self) -> int:
        """Primary store size reported by ``_stats``."""
        stats = self._request("GET", f"/{quote(self.index_name)}/_stats/store")
        total = cast("dict[str, dict[str, dict[str, int]]]", stats["_all"])
        return int(total["primaries"]["store"]["size_in_bytes"])


def _hit(raw: object) -> ScoredChunk:
    item = cast("dict[str, object]", raw)
    source = cast("dict[str, object]", item["_source"])
    published = source.get("published_at")
    return ScoredChunk(
        chunk=ChunkRef(
            article_id=UUID(str(source["article_id"])),
            content_hash=str(source["content_hash"]),
            chunker_version=str(source["chunker_version"]),
            chunk_index=int(cast("int", source["chunk_index"])),
            start_offset=int(cast("int", source["start_offset"])),
            end_offset=int(cast("int", source["end_offset"])),
        ),
        score=float(cast("float", item["_score"])),
        source_published_at=None
        if published is None
        else datetime.fromisoformat(str(published)),
    )
