"""Search configurations under test, built only from application ports.

Concrete adapters (Elasticsearch, PostgreSQL, models) are injected by the
CLI; this package never imports them. Each index-backed system also indexes
*trap* documents (other scope, tombstoned, superseded) that must never be
returned; any hit on one counts as a scope violation.
"""

import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.reranker import (
    RerankCandidate,
    RerankerPort,
    RerankOutput,
    RerankRequest,
)
from backend.rag.application.ports.retrieval import RetrievalScope, RetrievalStore
from backend.rag.application.ports.search_index import SearchIndexPort
from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.application.use_cases.retrieve import (
    bundled_retrieval_policy,
    call_with_slice,
)
from backend.rag.application.use_cases.search_projection import (
    ProjectionChunker,
    ProjectRevision,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import DEFAULT_POLICY, chunk_document
from backend.rag.domain.fusion import rank_lexical
from backend.rag.domain.paragraph_chunking import DEFAULT_PARAGRAPH_POLICY
from backend.rag.domain.retrieval import LexicalLimits, QueryControl
from backend.rag.domain.search_index import (
    FusedHit,
    GenerationStatus,
    HybridSearchConfig,
    IndexGeneration,
    SearchChunkDocument,
    SearchFilters,
    SearchMode,
)
from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.search_eval.dataset import FIXTURE_AS_OF, EvalQuestion
from backend.rag.evaluation.search_eval.scoring import RankedPassage

if TYPE_CHECKING:
    from backend.rag.evaluation.search_eval.stages import EvidenceStages

SCOPE = "fixture-internal"
RERANK_WINDOW = 40
RERANK_TIMEOUT_SECONDS = 30.0

# (article_id, content_hash, char_start, char_end) of every indexed chunk.
ChunkSpan = tuple[UUID, str, int, int]


def chunk_length_summary(spans: Sequence[ChunkSpan]) -> dict[str, Json]:
    """Chunk count and code-point length distribution (deterministic)."""
    lengths = sorted(end - start for _, _, start, end in spans)
    if not lengths:
        return {"chunks": 0}
    quantiles = statistics.quantiles(lengths, n=20, method="inclusive")
    return {
        "chunks": len(lengths),
        "min": lengths[0],
        "p05": round(quantiles[0], 1),
        "p50": round(statistics.median(lengths), 1),
        "p95": round(quantiles[18], 1),
        "max": lengths[-1],
        "mean": round(statistics.fmean(lengths), 1),
    }


@dataclass(frozen=True, slots=True)
class SystemAnswer:
    """Ranked passages plus timings (ms) and rerank outcome."""

    ranked: tuple[RankedPassage, ...]
    timings_ms: dict[str, float]
    query_tokens: int
    rerank_status: str | None = None
    trap_hits: int = 0
    evidence: "EvidenceStages | None" = None


class SearchSystem(Protocol):
    """One configuration: index the corpus once, answer every question."""

    @property
    def name(self) -> str:
        """Stable configuration name used in the report."""
        ...

    def describe(self) -> dict[str, Json]:
        """Everything that identifies the configuration (no secrets)."""
        ...

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]:
        """Index the corpus; returns indexing stats."""
        ...

    def search(self, question: EvalQuestion) -> SystemAnswer:
        """Ranked passages for one question."""
        ...

    def chunk_layout(self) -> tuple[ChunkSpan, ...]:
        """Offsets of every chunk this system can return (after prepare)."""
        ...

    def close(self) -> None:
        """Drop anything the system created."""
        ...


def _passage(fused: FusedHit, trap_ids: set[str]) -> RankedPassage:
    hit = fused.hit
    return RankedPassage(
        chunk_id=hit.chunk_id,
        article_id=hit.article_id,
        content_hash=hit.content_hash,
        chunk_version=hit.chunk_version,
        char_start=hit.char_start,
        char_end=hit.char_end,
        text=hit.text,
        fused_rank=fused.fused_rank,
        lexical_rank=fused.lexical_rank,
        semantic_rank=fused.semantic_rank,
        trap=str(hit.chunk_id) in trap_ids,
    )


def _traps(documents: Sequence[SearchChunkDocument]) -> list[SearchChunkDocument]:
    """Copies that must stay invisible: other scope, tombstoned, superseded."""
    base = list(documents[:6])
    traps: list[SearchChunkDocument] = []
    for index, document in enumerate(base):
        kind = index % 3
        traps.append(
            replace(
                document,
                chunk_id=uuid4(),
                access_scopes=("another-tenant",)
                if kind == 0
                else document.access_scopes,
                tombstoned=kind == 1,
                is_current=kind != 2,  # noqa: PLR2004
            )
        )
    return traps


@dataclass(slots=True)
class IndexSystem:
    """Lexical, semantic or hybrid search over a ``SearchIndexPort``."""

    label: str
    mode: SearchMode
    index: SearchIndexPort
    embedder: BatchEmbeddingPort | None
    config: HybridSearchConfig = field(default_factory=HybridSearchConfig)
    reranker: RerankerPort | None = None
    engine_version: str | None = None
    chunker: ProjectionChunker = DEFAULT_PARAGRAPH_POLICY
    analyzer: str | None = None
    timer: Callable[[], float] = time.perf_counter
    generation: IndexGeneration | None = None
    trap_ids: set[str] = field(default_factory=set[str])
    layout: tuple[ChunkSpan, ...] = ()

    @property
    def name(self) -> str:
        """Configuration name."""
        return self.label

    def describe(self) -> dict[str, Json]:
        """Mode, depths, fusion, embedding identity, chunker and reranker."""
        identity = None if self.embedder is None else self.embedder.identity
        reranker = None if self.reranker is None else self.reranker.identity
        return {
            "engine": self.index.engine,
            "engine_version": self.engine_version,
            "mode": self.mode.value,
            "bm25_depth": self.config.bm25_depth,
            "semantic_depth": self.config.semantic_depth,
            "fusion": f"rrf-k{self.config.rrf_k}",
            "candidate_pool": self.config.candidate_pool,
            "chunker_version": self.chunker.version,
            "analyzer": self.analyzer,
            "filters": "scope + per-question source/date, applied in-engine",
            "embedding_identity": None if identity is None else identity.key,
            "embedding_model": None if identity is None else identity.model,
            "embedding_revision": None if identity is None else identity.revision,
            "embedding_dimensions": None if identity is None else identity.dimensions,
            "reranker": None
            if reranker is None
            else f"{reranker.reranker_id}@{reranker.version}",
            "rerank_window": RERANK_WINDOW if reranker is not None else None,
            "index_generation": None
            if self.generation is None
            else self.generation.label,
        }

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]:
        """Project (chunk + embed) and bulk index the corpus plus trap copies."""
        identity = None if self.embedder is None else self.embedder.identity
        self.generation = IndexGeneration(
            generation_id=1,
            index_name="eventlens-rag-search-eval-"
            + re.sub(r"[^a-z0-9-]+", "-", self.label.lower())
            + f"-{uuid4().hex[:8]}",
            chunk_version=self.chunker.version,
            embedding_identity=None if identity is None else identity.key,
            embedding_model=None if identity is None else identity.model,
            embedding_dimensions=None if identity is None else identity.dimensions,
            status=GenerationStatus.ACTIVE,
        )
        started = self.timer()
        projector = ProjectRevision(self.embedder, None, policy=self.chunker)
        documents = projector.execute_many(
            [
                ProjectionRevision(
                    article_id=a.article_id, document=a, tombstoned=False, metadata=None
                )
                for a in articles
            ],
            self.generation,
        )
        embedded_ms = (self.timer() - started) * 1000.0
        traps = _traps(documents)
        self.trap_ids = {str(t.chunk_id) for t in traps}
        self.layout = tuple(
            (d.article_id, d.content_hash, d.char_start, d.char_end) for d in documents
        )
        self.index.ensure_index(self.generation)
        started = self.timer()
        result = self.index.bulk_index(self.generation, (*documents, *traps))
        bulk_ms = round((self.timer() - started) * 1000.0, 1)
        status = self.index.status(self.generation)
        return {
            "chunks": len(documents),
            "trap_documents": len(traps),
            "indexed": result.indexed,
            "failed": result.failed,
            "projection_ms": round(embedded_ms, 1),
            "bulk_ms": bulk_ms,
            "embedded_texts": projector.stats.embedded,
            "index_documents": status.documents,
            "index_store_bytes": status.store_bytes,
        }

    def chunk_layout(self) -> tuple[ChunkSpan, ...]:
        """Offsets of the projected (non-trap) chunks."""
        return self.layout

    def search(self, question: EvalQuestion) -> SystemAnswer:
        """Run the configured branches with the shared filters."""
        generation = self.generation
        assert generation is not None  # noqa: S101 - prepare() first
        wanted = question.filters
        filters = SearchFilters(
            scope_id=question.allowed_scope,
            source_domains=wanted.source_domains,
            published_after=wanted.published_after,
            published_before=wanted.published_before,
        )
        timings: dict[str, float] = {}
        started = self.timer()
        vector = None
        if self.mode in {SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID}:
            assert self.embedder is not None  # noqa: S101 - wired by the CLI
            vector = self.embedder.embed_query(question.query)
            timings["embed_query"] = (self.timer() - started) * 1000.0
        branch_started = self.timer()
        if self.mode is SearchMode.LEXICAL_ONLY:
            fused = self.index.hybrid_search(
                generation, question.query, None, filters, self.config
            )
        elif self.mode is SearchMode.SEMANTIC_ONLY:
            assert vector is not None  # noqa: S101
            hits = self.index.semantic_search(
                generation, vector, filters, self.config.semantic_depth
            )
            fused = tuple(
                FusedHit(h, None, h.rank, h.rank, 1.0 / (60 + h.rank), 0.0, 0.0)
                for h in hits[: self.config.candidate_pool]
            )
        else:
            fused = self.index.hybrid_search(
                generation, question.query, vector, filters, self.config
            )
        timings["search"] = (self.timer() - branch_started) * 1000.0
        ranked = tuple(_passage(f, self.trap_ids) for f in fused)
        status = None
        if self.reranker is not None and ranked:
            rerank_started = self.timer()
            ranked, status = self._rerank(question.query, ranked)
            timings["rerank"] = (self.timer() - rerank_started) * 1000.0
        timings["total"] = (self.timer() - started) * 1000.0
        return SystemAnswer(ranked, timings, len(question.query.split()), status)

    def _rerank(
        self, query: str, ranked: tuple[RankedPassage, ...]
    ) -> tuple[tuple[RankedPassage, ...], str]:
        reranker = self.reranker
        assert reranker is not None  # noqa: S101
        window, tail = ranked[:RERANK_WINDOW], ranked[RERANK_WINDOW:]
        request = RerankRequest(
            query,
            tuple(RerankCandidate(r.chunk_id, r.text) for r in window),
            RERANK_TIMEOUT_SECONDS,
        )
        slot = call_with_slice(
            lambda: reranker.rerank(request),
            RERANK_TIMEOUT_SECONDS,
            QueryControl(RERANK_TIMEOUT_SECONDS + 5),
        )
        if not slot.done:
            return ranked, "timeout"
        if slot.failed or not isinstance(slot.value, RerankOutput):
            return ranked, "degraded"
        scores = {str(cid): score for cid, score in slot.value.scores}
        ordered = sorted(
            window,
            key=lambda r: (
                -float(str(scores.get(str(r.chunk_id), "-inf"))),
                r.fused_rank,
            ),
        )
        return (
            tuple(replace(r, fused_rank=i) for i, r in enumerate((*ordered, *tail), 1)),
            "applied",
        )

    def close(self) -> None:
        """Delete the evaluation index."""
        if self.generation is not None:
            self.index.drop_index(self.generation)


@dataclass(slots=True)
class PostgresLexicalSystem:
    """Production PostgreSQL FTS branch (``simple`` lexemes, codepoint chunks)."""

    store: RetrievalStore
    persist: Callable[[ArticleDocument], None]
    as_of: datetime = FIXTURE_AS_OF
    timer: Callable[[], float] = time.perf_counter
    layout: tuple[ChunkSpan, ...] = ()

    @property
    def name(self) -> str:
        """Configuration name."""
        return "postgres-lexical"

    def describe(self) -> dict[str, Json]:
        """Policy and chunker of the PostgreSQL lexical branch."""
        policy = bundled_retrieval_policy()
        return {
            "engine": "postgresql",
            "mode": "lexical_only",
            "policy": policy.policy_id,
            "text_search_config": "simple",
            "chunker_version": DEFAULT_POLICY.version,
            "bm25_depth": policy.lexical.max_text_chunks,
            "freshness": "policy freshness bonus, query clock "
            + self.as_of.isoformat(),
            "filters": "per-question source/date via RetrievalScope",
            "embedding_identity": None,
            "reranker": None,
        }

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]:
        """Persist the corpus (the database must be isolated and empty)."""
        started = self.timer()
        for article in articles:
            self.persist(article)
        self.layout = tuple(
            (a.article_id, a.content_hash, c.start_offset, c.end_offset)
            for a in articles
            for c in chunk_document(a, DEFAULT_POLICY)
        )
        return {
            "persisted": len(articles),
            "persist_ms": round((self.timer() - started) * 1000.0, 1),
            "chunks": len(self.layout),
        }

    def chunk_layout(self) -> tuple[ChunkSpan, ...]:
        """Codepoint windows the lexical branch scores (same arithmetic)."""
        return self.layout

    def search(self, question: EvalQuestion) -> SystemAnswer:
        """Bounded FTS pool ranked by the frozen v3 policy."""
        policy = bundled_retrieval_policy()
        lexical = policy.lexical
        wanted = question.filters
        started = self.timer()
        page = self.store.lexical_chunks(
            question.query,
            RetrievalScope(
                wanted.source_domains,
                wanted.published_after,
                wanted.published_before,
                None,
            ),
            LexicalLimits(
                stopwords=policy.tokenizer.stopwords,
                max_terms=policy.tokenizer.max_query_terms,
                article_pool=lexical.article_pool,
                chunks_scanned_per_article=lexical.max_chunks_scanned_per_article,
                chunks_per_article=lexical.max_chunks_per_article,
                max_metadata_candidates=lexical.max_metadata_candidates,
                chunk_size=DEFAULT_POLICY.size,
                chunk_overlap=DEFAULT_POLICY.overlap,
            ),
        )
        ranked = rank_lexical(
            page,
            self.as_of,
            lexical,
            policy.freshness,
            policy.chunker_version,
        )[: lexical.max_text_chunks]
        texts = (
            self.store.chunk_texts(tuple(item.chunk for item in ranked))
            if ranked
            else ()
        )
        elapsed = (self.timer() - started) * 1000.0
        return SystemAnswer(
            tuple(
                RankedPassage(
                    chunk_id=item.chunk.chunk_id,
                    article_id=item.chunk.article_id,
                    content_hash=item.chunk.content_hash,
                    chunk_version=item.chunk.chunker_version,
                    char_start=item.chunk.start_offset,
                    char_end=item.chunk.end_offset,
                    text=text,
                    fused_rank=item.rank,
                    lexical_rank=item.rank,
                    semantic_rank=None,
                )
                for item, text in zip(ranked, texts, strict=True)
            ),
            {"search": elapsed, "total": elapsed},
            len(question.query.split()),
        )

    def close(self) -> None:
        """The CLI owns (and drops) the isolated database."""
