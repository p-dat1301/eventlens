"""Retrieval systems under comparison, built only from ports and domain code.

A  ``FtsSystem``       production PostgreSQL FTS branch (``RetrievalStore`` +
                       ``rank_lexical``, frozen clock), unchanged.
B  ``Bm25System``      ``ChunkLexicalIndex`` (Elasticsearch BM25 in benchmarks).
C  ``SemanticSystem``  ``TextEmbedder`` + exact ``ChunkVectorIndex``.
D  ``RrfSystem``       production ``fuse`` (RRF k=60) of two systems.
E  ``RerankSystem``    production rerank window/ordering with a ``RerankerPort``.

Every system returns at most ``depth`` chunks with at most ``per_article``
chunks per article, ordered with the production tie-breaks, so systems differ
only in how they score.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.rag.application.ports.reranker import (
    RerankCandidate,
    RerankerPort,
    RerankRequest,
)
from backend.rag.application.ports.retrieval import RetrievalScope, RetrievalStore
from backend.rag.application.ports.search_index import (
    ChunkLexicalIndex,
    ChunkVectorIndex,
    TextEmbedder,
)
from backend.rag.domain.chunking import ChunkingPolicy
from backend.rag.domain.fusion import (
    fuse,
    order_after_rerank,
    published_key,
    rank_lexical,
    validate_rerank_scores,
)
from backend.rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    LexicalLimits,
    RetrievalPolicy,
)
from backend.rag.domain.search_index import ScoredChunk

DEPTH = 50
PER_ARTICLE = 3
ENGINE_POOL = 200


class RetrievalSystem(Protocol):
    """One configuration under test."""

    @property
    def name(self) -> str:
        """Short stable configuration name."""
        ...

    def describe(self) -> dict[str, object]:
        """Every setting that changes ranking (models, analyzers, k, depths)."""
        ...

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """Ranked chunks (best first)."""
        ...


def cap(
    hits: Sequence[ScoredChunk], depth: int = DEPTH, per_article: int = PER_ARTICLE
) -> tuple[ScoredChunk, ...]:
    """Production branch shaping: per-article cap, then depth.

    Order is ``score DESC, published DESC NULLS LAST, article_id, chunk_id``.
    """
    ordered = sorted(
        hits,
        key=lambda hit: (
            -hit.score,
            published_key(hit.source_published_at),
            str(hit.chunk.article_id),
            str(hit.chunk.chunk_id),
        ),
    )
    taken: dict[UUID, int] = {}
    seen: set[UUID] = set()
    result: list[ScoredChunk] = []
    for hit in ordered:
        if hit.chunk.chunk_id in seen:
            continue
        count = taken.get(hit.chunk.article_id, 0)
        if count >= per_article:
            continue
        seen.add(hit.chunk.chunk_id)
        taken[hit.chunk.article_id] = count + 1
        result.append(hit)
        if len(result) >= depth:
            break
    return tuple(result)


@dataclass(frozen=True, slots=True)
class FtsSystem:
    """A: the production lexical branch exactly as ``RetrieveEvidence`` runs it."""

    store: RetrievalStore
    policy: RetrievalPolicy
    chunking: ChunkingPolicy
    now: datetime
    name: str = "A_fts"

    def describe(self) -> dict[str, object]:
        """Policy ID, tokenizer and frozen query clock."""
        return {
            "engine": "postgresql-fts",
            "policy_id": self.policy.policy_id,
            "tokenizer": self.policy.tokenizer.tokenizer_id,
            "fts_config": self.policy.tokenizer.fts_config,
            "document_rank": "ts_rank_cd (article pool only)",
            "chunk_score": self.policy.lexical.chunk_score,
            "freshness": self.policy.freshness.model_dump(),
            "query_clock": self.now.isoformat(),
            "note": "PostgreSQL FTS lexeme coverage, not BM25",
        }

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """One bounded lexical read, ranked by ``rank_lexical``."""
        lexical = self.policy.lexical
        page = self.store.lexical_chunks(
            query,
            scope,
            LexicalLimits(
                stopwords=self.policy.tokenizer.stopwords,
                max_terms=self.policy.tokenizer.max_query_terms,
                article_pool=lexical.article_pool,
                chunks_scanned_per_article=lexical.max_chunks_scanned_per_article,
                chunks_per_article=lexical.max_chunks_per_article,
                max_metadata_candidates=lexical.max_metadata_candidates,
                chunk_size=self.chunking.size,
                chunk_overlap=self.chunking.overlap,
            ),
        )
        ranked = rank_lexical(
            page, self.now, lexical, self.policy.freshness, self.policy.chunker_version
        )
        return tuple(
            ScoredChunk(item.chunk, item.score, item.source_published_at)
            for item in ranked
        )


@dataclass(slots=True)
class Bm25System:
    """B: BM25 chunk index (engine applies scope filters)."""

    index: ChunkLexicalIndex
    name: str = "B_bm25"
    _identity: dict[str, object] = field(default_factory=dict)

    def describe(self) -> dict[str, object]:
        """Engine version, analyzer ID/digest and BM25 parameters."""
        if not self._identity:
            identity = self.index.identity
            self._identity = {
                "engine": identity.engine,
                "engine_version": identity.engine_version,
                "analyzer_id": identity.analyzer_id,
                "analyzer_digest": identity.analyzer_digest,
                "similarity": identity.similarity,
                "query": "match (OR of analyzed terms)",
            }
        return dict(self._identity)

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """Engine top pool, then production shaping."""
        return cap(self.index.search(query, scope, ENGINE_POOL))


@dataclass(frozen=True, slots=True)
class SemanticSystem:
    """C: query embedding + exact cosine (no freshness, no ANN)."""

    embedder: TextEmbedder
    vectors: ChunkVectorIndex
    name: str

    def describe(self) -> dict[str, object]:
        """Model, pinned revision, dimension and input conventions."""
        identity = self.embedder.identity
        return {
            "engine": "pgvector-exact-cosine",
            "model": identity.model,
            "revision": identity.revision,
            "dimension": identity.dimension,
            "pooling": identity.pooling,
            "query_prefix": identity.query_prefix,
            "max_tokens": identity.max_tokens,
            "normalized": identity.normalized,
        }

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """Embed once, exact search, production shaping."""
        (vector,) = self.embedder.embed_queries([query])
        hits = self.vectors.search(self.embedder.identity, vector, scope, ENGINE_POOL)
        return cap(hits)


def _branch(hits: Sequence[ScoredChunk], branch: Branch) -> list[BranchCandidate]:
    return [
        BranchCandidate(
            chunk=hit.chunk,
            branch=branch,
            rank=index,
            score=hit.score,
            source_published_at=hit.source_published_at,
            text="",
            vector_generation=None,
        )
        for index, hit in enumerate(hits, start=1)
    ]


@dataclass(frozen=True, slots=True)
class RrfSystem:
    """D: production RRF of a lexical-side and a semantic-side system."""

    lexical: RetrievalSystem
    semantic: RetrievalSystem
    name: str
    k: int = 60

    def describe(self) -> dict[str, object]:
        """Both inputs and the fusion constant."""
        return {
            "fusion": f"rrf-k{self.k}",
            "lexical": {"name": self.lexical.name, **self.lexical.describe()},
            "semantic": {"name": self.semantic.name, **self.semantic.describe()},
        }

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """Fuse both branch rankings (missing branch contributes 0)."""
        fused = fuse(
            _branch(self.lexical.search(query, scope), Branch.LEXICAL),
            _branch(self.semantic.search(query, scope), Branch.VECTOR),
            self.k,
        )
        return tuple(
            ScoredChunk(item.chunk, item.fused_score, item.source_published_at)
            for item in fused[:DEPTH]
        )


@dataclass(frozen=True, slots=True)
class RerankSystem:
    """E: rerank the first ``window`` fused chunks; the tail keeps fused order."""

    base: RetrievalSystem
    reranker: RerankerPort
    texts: Callable[[Sequence[UUID]], Mapping[UUID, str]]
    name: str
    window: int = 40

    def describe(self) -> dict[str, object]:
        """Base system, reranker identity and window."""
        identity = self.reranker.identity
        return {
            "base": {"name": self.base.name, **self.base.describe()},
            "reranker": identity.reranker_id,
            "reranker_revision": identity.version,
            "window": self.window,
            "beyond_window": "appended_in_fused_order",
        }

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        """Score the window; invalid output keeps fused order (degraded)."""
        hits = self.base.search(query, scope)
        fused = fuse(_branch(hits, Branch.LEXICAL), (), 60)
        by_id = {hit.chunk.chunk_id: hit for hit in hits}
        window, tail = fused[: self.window], fused[self.window :]
        ids = [item.chunk.chunk_id for item in window]
        text = self.texts(ids)
        output = self.reranker.rerank(
            RerankRequest(
                query=query,
                candidates=tuple(RerankCandidate(i, text[i]) for i in ids),
                deadline_seconds=60.0,
            )
        )
        scores = validate_rerank_scores(ids, output.scores)
        ranked = order_after_rerank(window, tail, scores)
        return tuple(
            ScoredChunk(
                item.candidate.chunk,
                item.rerank_score
                if item.rerank_score is not None
                else by_id[item.candidate.chunk.chunk_id].score,
                item.candidate.source_published_at,
            )
            for item in ranked
        )
