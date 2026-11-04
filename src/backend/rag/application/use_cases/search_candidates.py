"""Search-index candidate source for ``RetrieveEvidence`` (hybrid BM25 + kNN).

Both branches run against the ACTIVE index generation with identical,
non-scoring filters (caller scope, source domains, publication window,
topics/entities, metadata-eligible article IDs, current and untombstoned
documents only). Index outages become ``CandidateSourceUnavailableError`` so
retrieval can fall back to PostgreSQL; an embedding failure or identity
mismatch only disables the semantic branch, exactly like the PostgreSQL path.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from backend.rag.application.ports.candidates import (
    CandidateBatch,
    CandidateSourceUnavailableError,
)
from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.ports.search_index import (
    SearchIndexError,
    SearchIndexPort,
    SearchIndexUnavailableError,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    ChunkRef,
    QueryControl,
    RetrievalError,
    VectorBranchStatus,
    text_terms,
)
from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    BranchDiversity,
    FieldBoosts,
    RetrievalTuning,
    cap_per_article,
)
from backend.rag.domain.search_embedding import checked_vector
from backend.rag.domain.search_index import (
    HybridSearchConfig,
    IndexGeneration,
    SearchFilters,
    SearchHit,
)


def search_filters(
    scope: RetrievalScope, query: BriefingQuery, caller: CallerScope
) -> SearchFilters:
    """Map the retrieval scope and query filters onto index filters."""
    return SearchFilters(
        scope_id=caller.scope_id,
        source_domains=scope.source_domains,
        published_after=scope.published_after,
        published_before=scope.published_before,
        topics=tuple(str(topic) for topic in query.topic_filters),
        entity_ids=tuple(str(entity) for entity in query.entity_ids),
        allowed_article_ids=scope.allowed_article_ids,
    )


def _candidate(hit: SearchHit, branch: Branch) -> BranchCandidate:
    return BranchCandidate(
        chunk=ChunkRef(
            article_id=hit.article_id,
            content_hash=hit.content_hash,
            chunker_version=hit.chunk_version,
            chunk_index=hit.chunk_index,
            start_offset=hit.char_start,
            end_offset=hit.char_end,
        ),
        branch=branch,
        rank=hit.rank,
        score=hit.score,
        source_published_at=hit.published_at,
        text=hit.text,
        vector_generation=None,
    )


@dataclass(slots=True)
class _Diversity:
    """Per-article branch cap with at most one bounded backfill per branch call.

    The cap re-numbers ranks 1..n over the kept hits (like the PostgreSQL
    branches' per-article cap), so one long article cannot occupy a whole
    branch depth. A backfill re-queries once at ``backfill_depth`` only when
    the cap dropped hits, the branch filled its depth (more may exist) and
    enough deadline is left; the result is still cut to the original depth.
    """

    policy: BranchDiversity
    control: QueryControl
    dropped: int = 0
    backfill_calls: int = 0

    def fetch(
        self, search: Callable[[int], tuple[SearchHit, ...]], depth: int
    ) -> tuple[SearchHit, ...]:
        hits = search(depth)
        cap = self.policy.max_chunks_per_article
        if cap is None:
            return hits
        kept, dropped = cap_per_article(hits, lambda hit: hit.article_id, cap)
        wider = self.policy.backfill_depth
        if (
            dropped
            and wider is not None
            and wider > depth
            and len(hits) >= depth
            and self.policy.max_backfill_calls > 0
            and self.control.remaining() >= self.policy.min_remaining_seconds
        ):
            self.backfill_calls += 1
            kept, dropped = cap_per_article(
                search(wider), lambda hit: hit.article_id, cap
            )
        self.dropped += dropped
        return tuple(
            replace(hit, rank=rank) for rank, hit in enumerate(kept[:depth], start=1)
        )


@dataclass(frozen=True, slots=True)
class IndexedCandidateSource:
    """``CandidateSource`` over a ``SearchIndexPort`` (ACTIVE generation only)."""

    index: SearchIndexPort
    embedder: BatchEmbeddingPort | None
    generations: Callable[[], IndexGeneration | None]
    config: HybridSearchConfig = field(default_factory=HybridSearchConfig)
    timer: Callable[[], float] = time.perf_counter

    def _generation(self) -> IndexGeneration:
        try:
            generation = self.generations()
        except RetrievalError:
            raise
        except Exception as error:
            reason = "generation_unavailable"
            raise CandidateSourceUnavailableError(reason) from error
        if generation is None:
            reason = "no_active_generation"
            raise CandidateSourceUnavailableError(reason)
        return generation

    def candidates(  # noqa: PLR0913 - mirrors the CandidateSource port
        self,
        text: str,
        scope: RetrievalScope,
        query: BriefingQuery,
        caller: CallerScope,
        control: QueryControl,
        *,
        lexical: bool,
        semantic: bool,
        tuning: RetrievalTuning | None = None,
    ) -> CandidateBatch:
        """Filtered BM25 and kNN candidates of the ACTIVE generation."""
        tuning = tuning or BASELINE_TUNING
        generation = self._generation()
        filters = search_filters(scope, query, caller)
        boosts = None if tuning.boosts == FieldBoosts() else tuning.boosts
        storage_calls = 0
        lexical_hits: tuple[SearchHit, ...] = ()
        diverse = _Diversity(tuning.diversity, control)
        try:
            if lexical:
                control.admit()

                def bm25(depth: int) -> tuple[SearchHit, ...]:
                    control.admit()
                    return self.index.lexical_search(
                        generation, text, filters, depth, boosts
                    )

                lexical_hits = diverse.fetch(bm25, self.config.bm25_depth)
                storage_calls += 1
            control.admit()
            vector_hits, status, embedding_calls, calls = self._semantic(
                generation, text, filters, control, enabled=semantic, diverse=diverse
            )
        except SearchIndexUnavailableError as error:
            raise CandidateSourceUnavailableError(error.code) from error
        except SearchIndexError as error:
            raise CandidateSourceUnavailableError(error.code) from error
        control.admit()
        storage_calls += diverse.backfill_calls
        current = generation.generation_id
        return CandidateBatch(
            lexical=tuple(
                _candidate(hit, Branch.LEXICAL)
                for hit in lexical_hits
                if hit.generation_id == current
            ),
            vector=tuple(
                _candidate(hit, Branch.VECTOR)
                for hit in vector_hits
                if hit.generation_id == current
            ),
            vector_status=status,
            term_count=len(text_terms(text)),
            backend=self.index.engine,
            generation=generation.label,
            chunker_version=generation.chunk_version,
            embedding_identity=generation.embedding_identity,
            storage_calls=storage_calls + calls,
            embedding_calls=embedding_calls,
            branch_cap_dropped=diverse.dropped,
            backfill_calls=diverse.backfill_calls,
        )

    def _semantic(  # noqa: PLR0913 - branch inputs plus the diversity helper
        self,
        generation: IndexGeneration,
        text: str,
        filters: SearchFilters,
        control: QueryControl,
        *,
        enabled: bool,
        diverse: "_Diversity",
    ) -> tuple[tuple[SearchHit, ...], VectorBranchStatus, int, int]:
        """Semantic kNN branch: hits, status, embedding calls, storage calls."""
        embedder = self.embedder
        if not enabled or embedder is None or generation.embedding_identity is None:
            return (), VectorBranchStatus.DISABLED, 0, 0
        identity = embedder.identity
        if (
            identity.key != generation.embedding_identity
            or identity.dimensions != generation.embedding_dimensions
        ):
            return (), VectorBranchStatus.EMBEDDING_INCOMPATIBLE, 0, 0
        try:
            vector = checked_vector(embedder.embed_query(text), identity.dimensions)
        except RetrievalError:
            raise
        except Exception:  # noqa: BLE001 - embedding failure means lexical-only
            return (), VectorBranchStatus.EMBEDDING_FAILED, 1, 0
        control.admit()

        def knn(depth: int) -> tuple[SearchHit, ...]:
            control.admit()
            return self.index.semantic_search(generation, vector, filters, depth)

        hits = diverse.fetch(knn, self.config.semantic_depth)
        return hits, VectorBranchStatus.EXECUTED, 1, 1
