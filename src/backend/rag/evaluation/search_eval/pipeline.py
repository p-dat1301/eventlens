"""Product retrieval pipeline under test: ``RetrieveEvidence`` end to end.

A configuration named ``<base>#<profile>`` (``es-hybrid@paragraph-900-1.v1#div3``,
``postgres-lexical#baseline``) runs the production use case with the named
retrieval tuning and measures three points on the same question:

A. candidates: the fused (deduplicated, reranked) list ``RetrieveEvidence``
   hands to selection; scored with the existing ranking metrics;
B. selection: the spans ``RetrieveEvidence.select`` keeps for the first
   ``result_limit`` articles (per-article cap, overlap filter), before the
   PostgreSQL final check;
C. payload: the spans actually serialized into the evidence context after
   the final current/tombstone/scope check and token packing.

Index configurations project the corpus into the index (with trap copies)
through the existing ``IndexSystem``; the final check reads the injected
truth store (PostgreSQL for handoff runs, else an in-memory copy of the
corpus). Labels never reach the pipeline: only query text and filters do.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol, cast
from uuid import UUID, uuid5

from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.retrieval import RetrievalScope, RetrievalStore
from backend.rag.application.ports.search_index import SearchIndexPort
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.search_candidates import (
    IndexedCandidateSource,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    LexicalLimits,
    LexicalPage,
    QueryControl,
    RankedCandidate,
    RetrievalResult,
)
from backend.rag.domain.retrieval_tuning import FieldBoosts, RetrievalTuning
from backend.rag.domain.search_index import (
    HybridSearchConfig,
    IndexGeneration,
    SearchFilters,
    SearchHit,
    SearchMode,
)
from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.search_eval.dataset import EvalQuestion
from backend.rag.evaluation.search_eval.scoring import RankedPassage
from backend.rag.evaluation.search_eval.stages import EvidenceStages, StageSpan
from backend.rag.evaluation.search_eval.systems import (
    SCOPE,
    ChunkSpan,
    IndexSystem,
    SystemAnswer,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore

PIPELINE: Final = "retrieve-evidence.v1"
PAYLOAD_NAMESPACE: Final = UUID("6b1f0d2e-3c4a-4e5f-8a9b-0c1d2e3f4a5b")
RESULT_LIMIT = 5  # public API default (BriefingRequest.result_limit)
CONTEXT_BUDGET = 12_000  # public API default; never raised by a profile
DEADLINE_SECONDS = 60.0
CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id=SCOPE,
    policy_id="fixture-policy",
    policy_version="v1",
)


class _Base(Protocol):
    @property
    def name(self) -> str: ...

    def describe(self) -> dict[str, Json]: ...

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]: ...

    def chunk_layout(self) -> tuple[ChunkSpan, ...]: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class SharedBase:
    """One prepared base system shared by every profile that names it.

    The index (and its embeddings) is built once; the last user closes it.
    Profiles over one base differ only in query-time tuning, so sharing keeps
    the comparison on identical chunks, embeddings and index statistics.
    """

    system: _Base
    users: int = 0
    stats: dict[str, Json] | None = None

    @property
    def name(self) -> str:
        """Base configuration name."""
        return self.system.name

    def describe(self) -> dict[str, Json]:
        """Base identity."""
        return self.system.describe()

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]:
        """Prepare once; later users get the same stats marked as shared."""
        if self.stats is None:
            self.stats = self.system.prepare(articles)
            return dict(self.stats)
        return {**self.stats, "shared_index": True}

    def chunk_layout(self) -> tuple[ChunkSpan, ...]:
        """Chunks of the base."""
        return self.system.chunk_layout()

    def close(self) -> None:
        """Close when the last user is done."""
        self.users -= 1
        if self.users <= 0:
            self.system.close()


class _TrapCountingIndex:
    """Delegating index that counts hits on trap chunk IDs (scope violations).

    Trap copies share offsets with real chunks, so after ``ChunkRef`` they are
    indistinguishable; they are counted here, on the raw index hits.
    """

    def __init__(
        self, inner: SearchIndexPort, trap_ids: Callable[[], set[str]]
    ) -> None:
        self.inner: SearchIndexPort = inner
        self.trap_ids: Callable[[], set[str]] = trap_ids
        self.hits: int = 0
        # Raw branch hits of the current query (reset per question): article,
        # offsets, branch rank, trap flag. Text-free.
        self.branches: dict[str, list[Json]] = {}

    def __getattr__(self, name: str) -> object:
        return cast("object", getattr(self.inner, name))

    def _count(self, hits: tuple[SearchHit, ...], branch: str) -> tuple[SearchHit, ...]:
        traps = self.trap_ids()
        self.hits += sum(1 for hit in hits if str(hit.chunk_id) in traps)
        # A backfill re-query replaces the first answer of the same branch.
        self.branches[branch] = [
            [
                str(hit.article_id),
                hit.char_start,
                hit.char_end,
                hit.rank,
                str(hit.chunk_id) in traps,
            ]
            for hit in hits
        ]
        return hits

    def lexical_search(
        self,
        generation: IndexGeneration,
        query: str,
        filters: SearchFilters,
        depth: int,
        boosts: FieldBoosts | None = None,
    ) -> tuple[SearchHit, ...]:
        """Counted BM25 branch."""
        return self._count(
            self.inner.lexical_search(generation, query, filters, depth, boosts),
            "lexical",
        )

    def semantic_search(
        self,
        generation: IndexGeneration,
        vector: tuple[float, ...],
        filters: SearchFilters,
        depth: int,
    ) -> tuple[SearchHit, ...]:
        """Counted kNN branch."""
        return self._count(
            self.inner.semantic_search(generation, vector, filters, depth),
            "semantic",
        )


class _TimedEmbedder:
    """Delegating embedder that times query embeddings (per query, ms)."""

    def __init__(self, inner: BatchEmbeddingPort, timer: Callable[[], float]) -> None:
        self.inner: BatchEmbeddingPort = inner
        self.timer: Callable[[], float] = timer
        self.elapsed_ms: float = 0.0
        self.calls: int = 0

    def __getattr__(self, name: str) -> object:
        return cast("object", getattr(self.inner, name))

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Timed query embedding."""
        started = self.timer()
        try:
            return self.inner.embed_query(text)
        finally:
            self.elapsed_ms += (self.timer() - started) * 1000.0
            self.calls += 1


class _RecordingStore:
    """Delegating truth store that keeps the PostgreSQL lexical pool per query.

    The FTS branch ranks and cuts ``LexicalPage.chunks`` inside
    ``RetrieveEvidence``; recording the page shows which windows were
    candidates before that cut. Text-free (article, offsets).
    """

    def __init__(self, inner: RetrievalStore) -> None:
        self.inner: RetrievalStore = inner
        self.lexical_pool: list[Json] | None = None
        self.lexical_truncated: int = 0

    def __getattr__(self, name: str) -> object:
        return cast("object", getattr(self.inner, name))

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Recorded FTS pool."""
        page = self.inner.lexical_chunks(query, scope, limits)
        self.lexical_pool = [
            [str(c.article_id), c.start_offset, c.end_offset] for c in page.chunks
        ]
        self.lexical_truncated = page.truncated_articles
        return page


def _query(question: EvalQuestion, result_limit: int) -> BriefingQuery:
    wanted = question.filters
    return BriefingQuery.model_validate(
        {
            "schema_version": "briefing_query.v1",
            "query": question.query,
            "source_domains": wanted.source_domains,
            "published_after": wanted.published_after,
            "published_before": wanted.published_before,
            "publication_time_basis": "source_published_at",
            "result_limit": result_limit,
            "context_budget": CONTEXT_BUDGET,
        }
    )


def _passage(position: int, item: RankedCandidate) -> RankedPassage:
    candidate = item.candidate
    chunk = candidate.chunk
    return RankedPassage(
        chunk_id=chunk.chunk_id,
        article_id=chunk.article_id,
        content_hash=chunk.content_hash,
        chunk_version=chunk.chunker_version,
        char_start=chunk.start_offset,
        char_end=chunk.end_offset,
        text=candidate.text,
        fused_rank=position,
        lexical_rank=candidate.lexical_rank,
        semantic_rank=candidate.vector_rank,
    )


@dataclass(slots=True)
class PipelineSystem:
    """``RetrieveEvidence`` with one tuning profile over a prepared base system."""

    label: str
    base: _Base
    tuning: RetrievalTuning
    as_of: datetime
    truth: Callable[[Sequence[ArticleDocument]], RetrievalStore] | None = None
    timer: Callable[[], float] = time.perf_counter
    retriever: RetrieveEvidence | None = None
    traps: _TrapCountingIndex | None = None
    recorder: _RecordingStore | None = None
    query_embedder: _TimedEmbedder | None = None
    options: RetrievalOptions = field(default_factory=RetrievalOptions)
    result_limit: int = RESULT_LIMIT

    @property
    def name(self) -> str:
        """Configuration name (``base#profile``)."""
        return self.label

    def describe(self) -> dict[str, Json]:
        """Base identity + pipeline, effective tuning, budget and clock."""
        policy = bundled_retrieval_policy()
        tuning = cast("Json", json.loads(json.dumps(self.tuning.describe())))
        return {
            **self.base.describe(),
            "pipeline": PIPELINE,
            "retrieval_policy": policy.policy_id,
            "fusion": f"weighted-rrf-k{policy.rrf.k}",
            "effective_weights": {
                "bm25": self.tuning.weights.lexical
                if self.options.lexical_enabled
                else 0.0,
                "semantic": self.tuning.weights.semantic
                if self.options.vector_enabled
                else 0.0,
            },
            "tuning": tuning,
            "bm25_field_boosts": (
                f"title^{self.tuning.boosts.title:g},"
                f"sapo^{self.tuning.boosts.sapo:g},text^{self.tuning.boosts.text:g}"
            ),
            "result_limit": self.result_limit,
            "context_budget": CONTEXT_BUDGET,
            "token_counter": "fixture-regex-tokens@v1",
            "query_clock": self.as_of.isoformat(),
            "rerank": "off (identity)",
        }

    def prepare(self, articles: Sequence[ArticleDocument]) -> dict[str, Json]:
        """Prepare the base (index + traps / database), then wire the use case."""
        stats = self.base.prepare(articles)
        store: RetrievalStore
        if self.truth is not None:
            store = self.truth(articles)
        else:
            memory = InMemoryRetrievalStore()
            for article in articles:
                memory.add(article)
            store = memory
        self.recorder = _RecordingStore(store)
        store = cast("RetrievalStore", cast("object", self.recorder))
        source = None
        inner = self.base.system if isinstance(self.base, SharedBase) else self.base
        if isinstance(inner, IndexSystem):
            base = inner
            self.traps = _TrapCountingIndex(base.index, lambda: base.trap_ids)
            generation = base.generation
            self.query_embedder = (
                None
                if base.embedder is None
                else _TimedEmbedder(base.embedder, self.timer)
            )
            source = IndexedCandidateSource(
                index=cast("SearchIndexPort", cast("object", self.traps)),
                embedder=None
                if base.embedder is None
                else cast("BatchEmbeddingPort", cast("object", self.query_embedder)),
                generations=lambda: generation,
                config=HybridSearchConfig(
                    bm25_depth=base.config.bm25_depth,
                    semantic_depth=base.config.semantic_depth,
                ),
            )
            self.options = RetrievalOptions(
                lexical_enabled=base.mode is not SearchMode.SEMANTIC_ONLY,
                vector_enabled=base.mode is not SearchMode.LEXICAL_ONLY,
                rerank_enabled=False,
            )
        else:
            self.options = RetrievalOptions(vector_enabled=False, rerank_enabled=False)
        as_of = self.as_of
        self.retriever = RetrieveEvidence(
            store=store,
            embeddings=None,
            embedding_identity=EmbeddingIdentity(),
            reranker=None,
            tokens=FixtureTokenCounter(),
            access=FixtureAccessPolicy(),
            policy=bundled_retrieval_policy(),
            clock=lambda: as_of,
            timer=self.timer,
            candidates=source,
            fallback_to_store=False,
            tuning=self.tuning,
        )
        return stats

    def chunk_layout(self) -> tuple[ChunkSpan, ...]:
        """Chunks the base system can return."""
        return self.base.chunk_layout()

    def close(self) -> None:
        """Drop what the base created."""
        self.base.close()

    def search(self, question: EvalQuestion) -> SystemAnswer:
        """A/B/C for one question through the phased use case."""
        retriever = self.retriever
        assert retriever is not None  # noqa: S101 - prepare() first
        traps_before = 0 if self.traps is None else self.traps.hits
        if self.traps is not None:
            self.traps.branches = {}
        if self.recorder is not None:
            self.recorder.lexical_pool = None
            self.recorder.lexical_truncated = 0
        if self.query_embedder is not None:
            self.query_embedder.elapsed_ms = 0.0
        started = self.timer()
        control = QueryControl(DEADLINE_SECONDS)
        draft = retriever.fuse(
            retriever.retrieve_primary(
                retriever.apply_filters(
                    _query(question, self.result_limit), CALLER, control, self.options
                )
            )
        )
        ranked = draft.ranked or ()
        selection = retriever.select(ranked)
        selected = tuple(
            StageSpan(
                str(item.candidate.chunk.article_id),
                item.candidate.chunk.content_hash,
                item.candidate.chunk.start_offset,
                item.candidate.chunk.end_offset,
            )
            for _, items in selection.groups[: self.result_limit]
            for item in items
        )
        result = retriever.assemble(draft)
        total = (self.timer() - started) * 1000.0
        payload = tuple(
            StageSpan(
                str(article.article_id),
                article.content_hash,
                span.span.start_offset,
                span.span.end_offset,
            )
            for article in result.articles
            for span in article.spans
        )
        metrics = result.metrics
        timings: dict[str, float] = {"total": total}
        for stage in metrics.stages:
            key = f"stage:{stage.stage}"
            timings[key] = timings.get(key, 0.0) + stage.latency_ms
        if self.query_embedder is not None:
            # Inside stage:search_index and inside total; reported separately.
            timings["embed_query"] = self.query_embedder.elapsed_ms
        traps = 0 if self.traps is None else self.traps.hits - traps_before
        trace = self._trace(ranked, selection.groups, result, timings)
        return SystemAnswer(
            ranked=tuple(_passage(i, item) for i, item in enumerate(ranked, 1)),
            timings_ms=timings,
            query_tokens=len(question.query.split()),
            rerank_status=None,
            trap_hits=traps,
            evidence=EvidenceStages(
                selected=selected,
                payload=payload,
                payload_tokens=result.context.token_count,
                token_budget=result.context.token_budget,
                truncated_spans=result.context.truncated_spans,
                omitted_articles=result.context.omitted_articles,
                overlap_dropped=metrics.selection_overlap_dropped,
                cap_dropped=metrics.selection_cap_dropped,
                branch_cap_dropped=metrics.branch_cap_dropped,
                backfill_calls=metrics.backfill_calls,
                dropped_at_final_check=metrics.dropped_at_final_check,
                storage_calls=metrics.storage_calls,
                embedding_calls=metrics.embedding_calls,
                freshness=metrics.freshness,
                freshness_boosted=metrics.freshness_boosted,
                empty_reason=None
                if result.empty_reason is None
                else result.empty_reason.value,
                fill_spans_sent=metrics.fill_spans_sent,
                freshness_max_promotion=metrics.freshness_max_promotion,
                trace=trace,
            ),
        )

    def _trace(
        self,
        ranked: Sequence[RankedCandidate],
        groups: Sequence[tuple[UUID, Sequence[RankedCandidate]]],
        result: RetrievalResult,
        timings: Mapping[str, float],
    ) -> dict[str, Json]:
        """Per-layer, text-free trace of one query (IDs, offsets, counters)."""
        policy = bundled_retrieval_policy()
        metrics = result.metrics
        branches: dict[str, Json] = {}
        if self.traps is not None:
            branches.update(self.traps.branches)
        if self.recorder is not None and self.recorder.lexical_pool is not None:
            branches["lexical_pool"] = self.recorder.lexical_pool
            branches["lexical_truncated_articles"] = self.recorder.lexical_truncated
        article_cap = min(self.result_limit, policy.output.max_articles)
        return {
            "branches": branches,
            "fused": [
                [
                    str(item.candidate.chunk.article_id),
                    item.candidate.chunk.start_offset,
                    item.candidate.chunk.end_offset,
                    item.candidate.lexical_rank,
                    item.candidate.vector_rank,
                ]
                for item in ranked
            ],
            "groups": [
                [
                    str(article_id),
                    [
                        [i.candidate.chunk.start_offset, i.candidate.chunk.end_offset]
                        for i in items
                    ],
                ]
                for article_id, items in groups
            ],
            "result_limit": article_cap,
            "final_check_pool": article_cap
            * policy.aggregation.final_check_pool_multiplier,
            "max_spans_per_article": self.tuning.selection.max_spans_per_article
            or policy.aggregation.max_spans_per_article,
            "payload": [
                [
                    str(article.article_id),
                    span.span.start_offset,
                    span.span.end_offset,
                    span.truncated,
                ]
                for article in result.articles
                for span in article.spans
            ],
            "payload_duplicates": [
                [
                    str(article.article_id),
                    [str(d) for d in article.duplicate_article_ids],
                ]
                for article in result.articles
                if article.duplicate_article_ids
            ],
            "counters": {
                "suppressed_duplicates": metrics.suppressed_duplicates,
                "branch_cap_dropped": metrics.branch_cap_dropped,
                "selection_cap_dropped": metrics.selection_cap_dropped,
                "selection_overlap_dropped": metrics.selection_overlap_dropped,
                "dropped_at_final_check": metrics.dropped_at_final_check,
                "truncated_spans": result.context.truncated_spans,
                "omitted_articles": result.context.omitted_articles,
                "payload_tokens": result.context.token_count,
                "token_budget": result.context.token_budget,
                "storage_calls": metrics.storage_calls,
                "embedding_calls": metrics.embedding_calls,
                "lexical_truncated_articles": metrics.lexical_truncated_articles,
            },
            "empty_reason": None
            if result.empty_reason is None
            else result.empty_reason.value,
            "timings_ms": {k: round(v, 3) for k, v in timings.items()},
        }


def payload_passages(
    answer: SystemAnswer, articles: Mapping[UUID, ArticleDocument]
) -> tuple[RankedPassage, ...]:
    """Stage-C payload spans as passages (exact NFC slices), else stage A.

    Lets the synthesis benchmark send the model exactly what the product
    would send (after selection, final check and token packing).
    """
    if answer.evidence is None:
        return answer.ranked
    out: list[RankedPassage] = []
    for position, span in enumerate(answer.evidence.payload, start=1):
        article = articles[UUID(span.article_id)]
        out.append(
            RankedPassage(
                chunk_id=uuid5(
                    PAYLOAD_NAMESPACE, f"{span.article_id}|{span.start}|{span.end}"
                ),
                article_id=article.article_id,
                content_hash=span.content_hash,
                chunk_version="payload",
                char_start=span.start,
                char_end=span.end,
                text=article.normalized_content[span.start : span.end],
                fused_rank=position,
                lexical_rank=None,
                semantic_rank=None,
            )
        )
    return tuple(out)
