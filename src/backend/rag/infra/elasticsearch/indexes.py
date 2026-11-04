"""``SearchIndexPort`` over Elasticsearch (one index per generation).

Only indices whose name starts with the configured prefix are created or
deleted. Every write waits for refresh (``refresh=wait_for`` / ``true``)
before returning, so a returned write is searchable and the outbox may mark
its event DONE. Item-level bulk failures are counted, never ignored.
"""

from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import quote
from uuid import UUID

from backend.rag.application.ports.search_index import (
    BulkResult,
    IndexStatus,
    RevisionWrite,
    SearchHealth,
    SearchIndexError,
    SearchIndexUnavailableError,
    WriteReport,
)
from backend.rag.domain.retrieval_tuning import FieldBoosts
from backend.rag.domain.search_index import (
    FusedHit,
    HybridSearchConfig,
    IndexGeneration,
    RevisionDigest,
    SearchBranch,
    SearchChunkDocument,
    SearchFilters,
    SearchHit,
    fence_version,
    stamped,
)
from backend.rag.infra.elasticsearch.bm25 import bm25_query
from backend.rag.infra.elasticsearch.client import ElasticsearchClient, Json
from backend.rag.infra.elasticsearch.hybrid import fuse
from backend.rag.infra.elasticsearch.mappings import index_body
from backend.rag.infra.elasticsearch.projection import (
    SOURCE_FIELDS,
    from_hit,
    to_source,
)
from backend.rag.infra.elasticsearch.vector import knn_query

BULK_BATCH: Final = 500
DIGEST_PAGE: Final = 1000
NOT_FOUND: Final = 404
HTTP_SUCCESS_MIN: Final = 200
HTTP_SUCCESS_MAX: Final = 300


HTTP_CONFLICT: Final = 409
CHUNK: Final[Json] = {"term": {"doc_kind": "chunk"}}


def _fence_id(article_id: UUID) -> str:
    return f"fence:{article_id}"


def _own_chunks(writes: list[RevisionWrite]) -> Json:
    """Chunks each write itself produced (its article at exactly its version)."""
    return {
        "bool": {
            "filter": [CHUNK],
            "should": [
                {
                    "bool": {
                        "filter": [
                            {"term": {"article_id": str(write.article_id)}},
                            {"term": {"projection_version": write.version}},
                        ]
                    }
                }
                for write in writes
            ],
            "minimum_should_match": 1,
        }
    }


def _older_than(writes: list[RevisionWrite]) -> Json:
    """Chunks of each article with an older (or missing) projection version."""
    clauses: list[Json] = []
    for write in writes:
        article = {"term": {"article_id": str(write.article_id)}}
        clauses.append(
            {
                "bool": {
                    "filter": [
                        article,
                        {"range": {"projection_version": {"lt": write.version}}},
                    ]
                }
            }
        )
        clauses.append(
            {
                "bool": {
                    "filter": [article],
                    "must_not": [{"exists": {"field": "projection_version"}}],
                }
            }
        )
    return {"bool": {"filter": [CHUNK], "should": clauses, "minimum_should_match": 1}}


def _dict(value: object) -> Json:
    return cast("Json", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


@dataclass(frozen=True, slots=True)
class _ChunkBulkResult:
    """Chunk acknowledgements and articles needing a fence conflict check."""

    result: BulkResult
    conflicts: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class ElasticsearchSearchIndex:
    """Generation-scoped index operations over the plain-HTTP client."""

    client: ElasticsearchClient
    index_prefix: str = "eventlens-rag-search"

    @property
    def engine(self) -> str:
        """Engine label."""
        return "elasticsearch"

    def _name(self, generation: IndexGeneration) -> str:
        name = generation.index_name
        if not name.startswith(self.index_prefix):
            msg = "refusing to touch an index outside the owned prefix"
            raise SearchIndexError(msg)
        return quote(name, safe="")

    def ensure_index(self, generation: IndexGeneration) -> None:
        """Create the index with the versioned mapping when missing."""
        name = self._name(generation)
        status, _ = self.client.request("HEAD", f"/{name}", allow=(NOT_FOUND,))
        if status == NOT_FOUND:
            _ = self.client.request(
                "PUT", f"/{name}", index_body(generation.embedding_dimensions)
            )

    def drop_index(self, generation: IndexGeneration) -> None:
        """Delete the generation index (missing is fine)."""
        _ = self.client.request(
            "DELETE", f"/{self._name(generation)}", allow=(NOT_FOUND,)
        )

    def _bulk(
        self,
        name: str,
        documents: tuple[SearchChunkDocument, ...],
        *,
        fenced: bool = False,
    ) -> _ChunkBulkResult:
        """Version each chunk; a fenced conflict needs an article fence re-check.

        Physical ES versions are projection_version + 1, keeping the legacy
        projection version 0 positive for ES internal deletes. Equal versions
        may replay; older versions cannot overwrite a newer shared chunk ID.
        """
        indexed = 0
        failed = 0
        conflicts: set[UUID] = set()
        for start in range(0, len(documents), BULK_BATCH):
            batch = documents[start : start + BULK_BATCH]
            lines: list[dict[str, object]] = []
            for document in batch:
                lines.append(
                    {
                        "index": {
                            "_id": str(document.chunk_id),
                            "version": document.projection_version + 1,
                            "version_type": "external_gte",
                        }
                    }
                )
                lines.append(to_source(document))
            response = self.client.bulk(f"/{name}/_bulk?refresh=wait_for", lines)
            raw_items = _list(response.get("items"))
            if not raw_items or len(raw_items) != len(batch):
                msg = "elasticsearch bulk response is empty, missing, or truncated"
                raise SearchIndexError(msg)
            for document, item in zip(batch, raw_items, strict=True):
                action = _dict(_dict(item).get("index"))
                status = action.get("status")
                if not action or not isinstance(status, int):
                    failed += 1
                elif HTTP_SUCCESS_MIN <= status < HTTP_SUCCESS_MAX:
                    indexed += 1
                elif status == HTTP_CONFLICT and fenced:
                    conflicts.add(document.article_id)
                else:
                    failed += 1
        return _ChunkBulkResult(
            BulkResult(indexed=indexed, deleted=0, failed=failed),
            frozenset(conflicts),
        )

    def _delete_by_query(self, name: str, query: Json) -> int:
        _, response = self.client.request(
            "POST",
            f"/{name}/_delete_by_query?refresh=true&conflicts=proceed",
            {"query": query},
        )
        if _list(response.get("failures")):
            msg = "delete_by_query reported failures"
            raise SearchIndexError(msg)
        deleted = response.get("deleted")
        return deleted if isinstance(deleted, int) else 0

    def write_revisions(
        self, generation: IndexGeneration, writes: tuple[RevisionWrite, ...]
    ) -> WriteReport:
        """Fenced article writes: claim fence, write, re-check, clean older.

        1. Index one fence document per article (``_id = fence:<article>``)
           with ``version_type=external_gte``: a write older than the fence
           gets 409 and is superseded (never applied); a replay of the same
           version is accepted (idempotent).
        2. Bulk-write chunks with external versions too (``refresh=wait_for``),
           so a delayed write cannot overwrite a newer shared chunk ID.
        3. Re-read the fences: if a newer write overtook this one meanwhile,
           delete this write's own chunks (the newer writer's cleanup may have
           run before they were visible). A chunk conflict is superseded only
           when its own article's fence confirms a newer version.
        4. Delete chunks of the article with an older projection version.
        A failed step 1/2 never reaches cleanup: older chunks stay searchable.
        """
        if not writes:
            return WriteReport(applied=0, superseded=0, failed=0, indexed=0, deleted=0)
        name = self._name(generation)
        writes = stamped(writes)
        accepted, superseded, failed = self._claim_fences(name, writes)
        documents = tuple(d for write in accepted for d in write.documents)
        chunk_bulk = self._bulk(name, documents, fenced=True)
        written = chunk_bulk.result
        if written.failed or failed:
            return WriteReport(
                applied=0,
                superseded=superseded,
                failed=failed + (len(accepted) if written.failed else 0),
                indexed=written.indexed,
                deleted=0,
            )
        current, lost = self._recheck(name, accepted)
        unexplained = chunk_bulk.conflicts - {write.article_id for write in lost}
        if unexplained:
            # A conflict without a newer article fence is inconsistent state;
            # fail without cleanup so the outbox retries instead of losing data.
            return WriteReport(
                applied=0,
                superseded=superseded + len(lost),
                failed=len(unexplained),
                indexed=written.indexed,
                deleted=0,
            )
        deleted = 0
        if lost:
            deleted += self._delete_by_query(name, _own_chunks(lost))
        if current:
            deleted += self._delete_by_query(name, _older_than(current))
        return WriteReport(
            applied=len(current),
            superseded=superseded + len(lost),
            failed=0,
            indexed=written.indexed,
            deleted=deleted,
        )

    def _claim_fences(
        self, name: str, writes: tuple[RevisionWrite, ...]
    ) -> tuple[list[RevisionWrite], int, int]:
        lines: list[dict[str, object]] = []
        for write in writes:
            lines.append(
                {
                    "index": {
                        "_id": _fence_id(write.article_id),
                        "version": write.version,
                        "version_type": "external_gte",
                    }
                }
            )
            lines.append(
                {
                    "doc_kind": "fence",
                    "article_id": str(write.article_id),
                    "projection_version": write.version,
                }
            )
        response = self.client.bulk(f"/{name}/_bulk?refresh=wait_for", lines)
        items = _list(response.get("items"))
        if len(items) != len(writes):
            msg = "elasticsearch fence response is empty, missing, or truncated"
            raise SearchIndexError(msg)
        accepted: list[RevisionWrite] = []
        superseded = 0
        failed = 0
        for write, item in zip(writes, items, strict=True):
            status = _dict(_dict(item).get("index")).get("status")
            if (
                isinstance(status, int)
                and HTTP_SUCCESS_MIN <= status < HTTP_SUCCESS_MAX
            ):
                accepted.append(write)
            elif status == HTTP_CONFLICT:
                superseded += 1
            else:
                failed += 1
        return accepted, superseded, failed

    def _recheck(
        self, name: str, accepted: list[RevisionWrite]
    ) -> tuple[list[RevisionWrite], list[RevisionWrite]]:
        if not accepted:
            return [], []
        _, response = self.client.request(
            "POST",
            f"/{name}/_mget",
            {"ids": [_fence_id(write.article_id) for write in accepted]},
        )
        docs = _list(response.get("docs"))
        if len(docs) != len(accepted):
            msg = "elasticsearch fence re-check response is truncated"
            raise SearchIndexError(msg)
        current: list[RevisionWrite] = []
        lost: list[RevisionWrite] = []
        for write, doc in zip(accepted, docs, strict=True):
            version = _dict(doc).get("_version")
            if not isinstance(version, int):
                msg = "elasticsearch fence disappeared during the write"
                raise SearchIndexError(msg)
            (lost if version > write.version else current).append(write)
        return current, lost

    def upsert_revision(
        self,
        generation: IndexGeneration,
        article_id: UUID,
        documents: tuple[SearchChunkDocument, ...],
        *,
        version: int | None = None,
    ) -> BulkResult:
        """One fenced article write (version defaults to the documents')."""
        report = self.write_revisions(
            generation,
            (RevisionWrite(article_id, fence_version(documents, version), documents),),
        )
        return BulkResult(
            indexed=report.indexed,
            deleted=report.deleted,
            failed=(len(documents) or 1) if report.failed else 0,
            superseded=report.superseded,
        )

    def delete_article(
        self, generation: IndexGeneration, article_id: UUID, *, version: int
    ) -> int:
        """Fenced removal of every chunk of the article."""
        report = self.write_revisions(
            generation, (RevisionWrite(article_id, version, ()),)
        )
        if report.failed:
            msg = "elasticsearch refused the fenced delete"
            raise SearchIndexError(msg)
        return report.deleted

    def bulk_index(
        self, generation: IndexGeneration, documents: tuple[SearchChunkDocument, ...]
    ) -> BulkResult:
        """Bulk index; item failures are reported in ``failed``."""
        return self._bulk(self._name(generation), documents).result

    def _search(
        self, generation: IndexGeneration, body: Json, branch: SearchBranch
    ) -> tuple[SearchHit, ...]:
        name = self._name(generation)
        _, response = self.client.request(
            "POST", f"/{name}/_search", {**body, "_source": list(SOURCE_FIELDS)}
        )
        hits = _list(_dict(response.get("hits")).get("hits"))
        try:
            return tuple(
                from_hit(_dict(hit), branch, rank)
                for rank, hit in enumerate(hits, start=1)
            )
        except (TypeError, ValueError) as error:
            msg = "malformed search hit"
            raise SearchIndexError(msg) from error

    def lexical_search(
        self,
        generation: IndexGeneration,
        query: str,
        filters: SearchFilters,
        depth: int,
        boosts: FieldBoosts | None = None,
    ) -> tuple[SearchHit, ...]:
        """BM25 branch."""
        if filters.empty_range or not query.strip():
            return ()
        return self._search(
            generation,
            bm25_query(query, filters, depth, boosts),
            SearchBranch.LEXICAL,
        )

    def semantic_search(
        self,
        generation: IndexGeneration,
        vector: tuple[float, ...],
        filters: SearchFilters,
        depth: int,
    ) -> tuple[SearchHit, ...]:
        """KNN branch (same filters)."""
        if filters.empty_range:
            return ()
        if generation.embedding_dimensions != len(vector):
            msg = "query vector dimension differs from the generation"
            raise SearchIndexError(msg)
        return self._search(
            generation, knn_query(vector, filters, depth), SearchBranch.SEMANTIC
        )

    def hybrid_search(
        self,
        generation: IndexGeneration,
        query: str,
        vector: tuple[float, ...] | None,
        filters: SearchFilters,
        config: HybridSearchConfig,
    ) -> tuple[FusedHit, ...]:
        """Two filtered requests fused by RRF."""
        lexical = self.lexical_search(generation, query, filters, config.bm25_depth)
        semantic = (
            ()
            if vector is None
            else self.semantic_search(
                generation, vector, filters, config.semantic_depth
            )
        )
        return fuse(lexical, semantic, config)

    def health(self, generation: IndexGeneration | None) -> SearchHealth:
        """Never raises: an unreachable engine is reported, not thrown."""
        try:
            _, info = self.client.request("GET", "/")
        except SearchIndexError:
            return SearchHealth(
                reachable=False,
                engine=self.engine,
                engine_version=None,
                index_exists=False,
                documents=None,
                status="unreachable",
            )
        version = _dict(info.get("version")).get("number")
        version_text = version if isinstance(version, str) else None
        if generation is None:
            return SearchHealth(
                reachable=True,
                engine=self.engine,
                engine_version=version_text,
                index_exists=False,
                documents=None,
                status="no_generation",
            )
        try:
            name = self._name(generation)
            status, health = self.client.request(
                "GET", f"/_cluster/health/{name}", allow=(NOT_FOUND,)
            )
            if status == NOT_FOUND:
                return SearchHealth(
                    reachable=True,
                    engine=self.engine,
                    engine_version=version_text,
                    index_exists=False,
                    documents=None,
                    status="missing_index",
                )
            _, count = self.client.request(
                "POST", f"/{name}/_count", {"query": CHUNK}, allow=(NOT_FOUND,)
            )
        except SearchIndexError:
            return SearchHealth(
                reachable=True,
                engine=self.engine,
                engine_version=version_text,
                index_exists=False,
                documents=None,
                status="error",
            )
        documents = count.get("count")
        color = health.get("status")
        return SearchHealth(
            reachable=True,
            engine=self.engine,
            engine_version=version_text,
            index_exists=True,
            documents=documents if isinstance(documents, int) else None,
            status=color if isinstance(color, str) else "unknown",
        )

    def status(self, generation: IndexGeneration) -> IndexStatus:
        """Document count plus distinct revisions."""
        name = self._name(generation)
        _, count = self.client.request("POST", f"/{name}/_count", {"query": CHUNK})
        _, revisions = self.client.request(
            "POST",
            f"/{name}/_search",
            {
                "size": 0,
                "query": CHUNK,
                "aggs": {"revisions": {"cardinality": {"field": "revision_id"}}},
            },
        )
        value = _dict(_dict(revisions.get("aggregations")).get("revisions")).get(
            "value"
        )
        documents = count.get("count")
        _, stats = self.client.request("GET", f"/{name}/_stats/store")
        size = _dict(
            _dict(_dict(_dict(stats.get("_all")).get("primaries")).get("store"))
        ).get("size_in_bytes")
        return IndexStatus(
            generation,
            documents if isinstance(documents, int) else 0,
            value if isinstance(value, int) else 0,
            size if isinstance(size, int) else None,
        )

    def revision_digests(
        self, generation: IndexGeneration
    ) -> tuple[RevisionDigest, ...]:
        """Composite aggregation over (article_id, content_hash), paged."""
        name = self._name(generation)
        digests: list[RevisionDigest] = []
        after: Json | None = None
        while True:
            composite: Json = {
                "size": DIGEST_PAGE,
                "sources": [
                    {"article_id": {"terms": {"field": "article_id"}}},
                    {"content_hash": {"terms": {"field": "content_hash"}}},
                ],
            }
            if after is not None:
                composite["after"] = after
            _, response = self.client.request(
                "POST",
                f"/{name}/_search",
                {
                    "size": 0,
                    "query": CHUNK,
                    "aggs": {"revisions": {"composite": composite}},
                },
            )
            aggregation = _dict(_dict(response.get("aggregations")).get("revisions"))
            buckets = _list(aggregation.get("buckets"))
            for bucket in buckets:
                key = _dict(_dict(bucket).get("key"))
                count = _dict(bucket).get("doc_count")
                article = key.get("article_id")
                content_hash = key.get("content_hash")
                if not isinstance(article, str) or not isinstance(content_hash, str):
                    msg = "malformed revision aggregation"
                    raise SearchIndexError(msg)
                digests.append(
                    RevisionDigest(
                        UUID(article),
                        content_hash,
                        count if isinstance(count, int) else 0,
                    )
                )
            next_key = aggregation.get("after_key")
            if not buckets or not isinstance(next_key, dict):
                break
            after = cast("Json", next_key)
        return tuple(digests)


def ping(client: ElasticsearchClient) -> bool:
    """True when the cluster answers ``GET /``."""
    try:
        _ = client.request("GET", "/")
    except SearchIndexUnavailableError:
        return False
    return True
