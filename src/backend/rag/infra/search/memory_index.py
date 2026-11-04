"""In-memory ``SearchIndexPort`` (unit tests and the CI search evaluation).

BM25 (k1=1.2, b=0.75) per field with the same boosts as the Elasticsearch
mapping (title 3, sapo 1.5, text 1) summed like ``multi_match most_fields``;
tokens are casefolded words plus accent-folded variants, mirroring the
``vi_folding`` analyzer. Semantic search is exact cosine over stored vectors.
Filters are applied before ranking in both branches. Fault switches simulate
an unavailable engine or a write that dies part-way.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
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
    rrf_fuse,
    stamped,
)
from backend.rag.infra.embeddings.fake_bge_m3 import fold

_WORD = re.compile(r"\w+")
K1: Final = 1.2
B: Final = 0.75
BOOSTS: Final = (("title", 3.0), ("sapo", 1.5), ("text", 1.0))


def analyze(text: str) -> list[str]:
    """Casefolded words plus their accent-folded variant when different."""
    tokens: list[str] = []
    for match in _WORD.finditer(text.casefold()):
        word = match.group(0)
        tokens.append(word)
        plain = fold(word)
        if plain != word:
            tokens.append(plain)
    return tokens


def _field(document: SearchChunkDocument, name: str) -> str:
    match name:
        case "title":
            return document.title
        case "sapo":
            return document.sapo
        case _:
            return document.text


def matches(document: SearchChunkDocument, filters: SearchFilters) -> bool:
    """Non-scoring filters shared by both branches."""
    checks = (
        document.is_current and not document.tombstoned,
        filters.scope_id in document.access_scopes,
        not filters.source_domains or document.source_domain in filters.source_domains,
        _in_window(document.published_at, filters),
        not filters.topics or bool(set(filters.topics) & set(document.topics)),
        not filters.entity_ids
        or bool(set(filters.entity_ids) & set(document.entity_ids)),
        filters.allowed_article_ids is None
        or document.article_id in filters.allowed_article_ids,
    )
    return all(checks) and not filters.empty_range


def _in_window(published: datetime | None, filters: SearchFilters) -> bool:
    if filters.published_after is None and filters.published_before is None:
        return True
    if published is None:
        return False
    if filters.published_after is not None and published < filters.published_after:
        return False
    return filters.published_before is None or published < filters.published_before


def _hit(
    document: SearchChunkDocument, branch: SearchBranch, rank: int, score: float
) -> SearchHit:
    return SearchHit(
        chunk_id=document.chunk_id,
        article_id=document.article_id,
        revision_id=document.revision_id,
        content_hash=document.content_hash,
        chunk_version=document.chunk_version,
        chunk_index=document.chunk_index,
        char_start=document.char_start,
        char_end=document.char_end,
        text=document.text,
        published_at=document.published_at,
        source_domain=document.source_domain,
        generation_id=document.generation_id,
        branch=branch,
        rank=rank,
        score=score,
    )


@dataclass(slots=True)
class InMemorySearchIndex:
    """Dict-backed index per generation, with fault injection."""

    unavailable: bool = False
    fail_after_writes: int | None = None
    indices: dict[str, dict[UUID, SearchChunkDocument]] = field(
        default_factory=dict[str, dict[UUID, SearchChunkDocument]]
    )
    fences: dict[str, dict[UUID, int]] = field(
        default_factory=dict[str, dict[UUID, int]]
    )
    writes: int = 0
    searches: int = 0

    @property
    def engine(self) -> str:
        """Engine label."""
        return "memory"

    def _check(self) -> None:
        if self.unavailable:
            msg = "memory index switched off"
            raise SearchIndexUnavailableError(msg)

    def _store(self, generation: IndexGeneration) -> dict[UUID, SearchChunkDocument]:
        self._check()
        store = self.indices.get(generation.index_name)
        if store is None:
            msg = "index does not exist"
            raise SearchIndexError(msg)
        return store

    def _write(self, count: int) -> None:
        if self.fail_after_writes is not None and self.writes + count > (
            self.fail_after_writes
        ):
            msg = "simulated interruption"
            raise SearchIndexUnavailableError(msg)
        self.writes += count

    def ensure_index(self, generation: IndexGeneration) -> None:
        """Create an empty index if absent."""
        self._check()
        _ = self.indices.setdefault(generation.index_name, {})

    def drop_index(self, generation: IndexGeneration) -> None:
        """Remove the index."""
        self._check()
        _ = self.indices.pop(generation.index_name, None)
        _ = self.fences.pop(generation.index_name, None)

    def write_revisions(
        self, generation: IndexGeneration, writes: tuple[RevisionWrite, ...]
    ) -> WriteReport:
        """Fenced per-article writes (same semantics as the ES adapter)."""
        store = self._store(generation)
        fences = self.fences.setdefault(generation.index_name, {})
        applied = superseded = indexed = deleted = 0
        for write in stamped(writes):
            fence = fences.get(write.article_id)
            if fence is not None and fence > write.version:
                superseded += 1
                continue
            self._write(max(1, len(write.documents)))
            fences[write.article_id] = write.version
            for document in write.documents:
                store[document.chunk_id] = document
            keep = {document.chunk_id for document in write.documents}
            stale = [
                chunk_id
                for chunk_id, document in store.items()
                if document.article_id == write.article_id
                and chunk_id not in keep
                and document.projection_version <= write.version
            ]
            for chunk_id in stale:
                del store[chunk_id]
            applied += 1
            indexed += len(write.documents)
            deleted += len(stale)
        return WriteReport(
            applied=applied,
            superseded=superseded,
            failed=0,
            indexed=indexed,
            deleted=deleted,
        )

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
            failed=report.failed,
            superseded=report.superseded,
        )

    def delete_article(
        self, generation: IndexGeneration, article_id: UUID, *, version: int
    ) -> int:
        """Fenced removal of every chunk of the article."""
        return self.write_revisions(
            generation, (RevisionWrite(article_id, version, ()),)
        ).deleted

    def bulk_index(
        self, generation: IndexGeneration, documents: tuple[SearchChunkDocument, ...]
    ) -> BulkResult:
        """Index documents (by chunk ID)."""
        store = self._store(generation)
        self._write(len(documents))
        for document in documents:
            store[document.chunk_id] = document
        return BulkResult(indexed=len(documents), deleted=0, failed=0)

    def lexical_search(
        self,
        generation: IndexGeneration,
        query: str,
        filters: SearchFilters,
        depth: int,
        boosts: FieldBoosts | None = None,
    ) -> tuple[SearchHit, ...]:
        """BM25 most_fields over title^3, sapo^1.5, text (or ``boosts``)."""
        weights = (
            BOOSTS
            if boosts is None
            else (("title", boosts.title), ("sapo", boosts.sapo), ("text", boosts.text))
        )
        store = self._store(generation)
        self.searches += 1
        terms = set(analyze(query))
        if not terms:
            return ()
        documents = list(store.values())
        stats = {name: self._field_stats(documents, name) for name, _ in BOOSTS}
        scored: list[tuple[float, str, SearchChunkDocument]] = []
        for document in documents:
            if not matches(document, filters):
                continue
            score = sum(
                boost * self._bm25(terms, analyze(_field(document, name)), stats[name])
                for name, boost in weights
            )
            if score > 0.0:
                scored.append((score, str(document.chunk_id), document))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            _hit(document, SearchBranch.LEXICAL, rank, score)
            for rank, (score, _, document) in enumerate(scored[:depth], start=1)
        )

    @staticmethod
    def _field_stats(
        documents: Iterable[SearchChunkDocument], name: str
    ) -> tuple[int, float, Counter[str]]:
        frequencies: Counter[str] = Counter()
        lengths = 0
        count = 0
        for document in documents:
            tokens = analyze(_field(document, name))
            lengths += len(tokens)
            count += 1
            frequencies.update(set(tokens))
        return count, (lengths / count if count else 0.0), frequencies

    @staticmethod
    def _bm25(
        terms: set[str], tokens: list[str], stats: tuple[int, float, Counter[str]]
    ) -> float:
        count, average, frequencies = stats
        if not tokens or average == 0.0:
            return 0.0
        tf = Counter(tokens)
        score = 0.0
        for term in terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            df = frequencies.get(term, 0)
            idf = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
            norm = freq + K1 * (1.0 - B + B * len(tokens) / average)
            score += idf * freq * (K1 + 1.0) / norm
        return score

    def semantic_search(
        self,
        generation: IndexGeneration,
        vector: tuple[float, ...],
        filters: SearchFilters,
        depth: int,
    ) -> tuple[SearchHit, ...]:
        """Exact cosine (vectors are unit length) with the same filters."""
        store = self._store(generation)
        self.searches += 1
        if generation.embedding_dimensions != len(vector):
            msg = "query vector dimension differs from the generation"
            raise SearchIndexError(msg)
        scored: list[tuple[float, str, SearchChunkDocument]] = []
        for document in store.values():
            if document.embedding is None or not matches(document, filters):
                continue
            score = sum(a * b for a, b in zip(vector, document.embedding, strict=True))
            scored.append((score, str(document.chunk_id), document))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            _hit(document, SearchBranch.SEMANTIC, rank, score)
            for rank, (score, _, document) in enumerate(scored[:depth], start=1)
        )

    def hybrid_search(
        self,
        generation: IndexGeneration,
        query: str,
        vector: tuple[float, ...] | None,
        filters: SearchFilters,
        config: HybridSearchConfig,
    ) -> tuple[FusedHit, ...]:
        """BM25 + kNN with identical filters, fused by RRF."""
        lexical = self.lexical_search(generation, query, filters, config.bm25_depth)
        semantic = (
            ()
            if vector is None
            else self.semantic_search(
                generation, vector, filters, config.semantic_depth
            )
        )
        return rrf_fuse(lexical, semantic, config.rrf_k, config.candidate_pool)

    def health(self, generation: IndexGeneration | None) -> SearchHealth:
        """Never raises."""
        if self.unavailable:
            return SearchHealth(
                reachable=False,
                engine=self.engine,
                engine_version=None,
                index_exists=False,
                documents=None,
                status="unreachable",
            )
        if generation is None:
            return SearchHealth(
                reachable=True,
                engine=self.engine,
                engine_version="1",
                index_exists=False,
                documents=None,
                status="no_generation",
            )
        store = self.indices.get(generation.index_name)
        return SearchHealth(
            reachable=True,
            engine=self.engine,
            engine_version="1",
            index_exists=store is not None,
            documents=None if store is None else len(store),
            status="green" if store is not None else "missing_index",
        )

    def status(self, generation: IndexGeneration) -> IndexStatus:
        """Document and revision counts."""
        store = self._store(generation)
        revisions = {(d.article_id, d.content_hash) for d in store.values()}
        return IndexStatus(generation, len(store), len(revisions))

    def revision_digests(
        self, generation: IndexGeneration
    ) -> tuple[RevisionDigest, ...]:
        """One digest per (article, content hash) held."""
        store = self._store(generation)
        counts: Counter[tuple[UUID, str]] = Counter(
            (d.article_id, d.content_hash) for d in store.values()
        )
        return tuple(
            RevisionDigest(article_id, content_hash, chunks)
            for (article_id, content_hash), chunks in sorted(
                counts.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        )
