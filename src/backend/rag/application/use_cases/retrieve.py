"""Hybrid retrieval use case (D35/D43).

```text
BriefingQuery + trusted CallerScope + QueryControl
  -> access + configuration check, query clock read once
  -> metadata eligibility (only for topic/entity filters)
  -> lexical FTS branch + compatible exact-cosine vector branch
  -> RRF (k=60) -> exact/near-duplicate suppression
  -> bounded rerank (identity by default) -> article aggregation
  -> one batched final current/access/metadata check
  -> token-budgeted evidence context + metrics
```

``QueryControl.admit`` runs before every storage, embedding and rerank call
and before each following step. Cancellation and the end-to-end deadline stop
the query with a typed error; only a rerank-specific timeout, error or invalid
output (while the query is still valid) degrades to fused order. Storage
failures propagate as ``RetrievalStorageError`` and are never reported as
empty results or rerank degradation.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.resources import files
from threading import Event, Thread
from typing import cast
from unicodedata import normalize
from uuid import UUID

from backend.rag.application.evidence_context import (
    EvidenceAssembler,
    PreparedArticle,
    PreparedSpan,
)
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.candidates import (
    CandidateSource,
    CandidateSourceUnavailableError,
)
from backend.rag.application.ports.embeddings import EmbeddingPort
from backend.rag.application.ports.reranker import (
    RerankCandidate,
    RerankerPort,
    RerankOutput,
    RerankRequest,
)
from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    RetrievalScope,
    RetrievalStore,
)
from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.article_scope import ArticleRestriction
from backend.rag.domain.chunking import DEFAULT_POLICY, ChunkingPolicy, chunk_bounds
from backend.rag.domain.embedding import EmbeddingIdentity, EmbeddingVector
from backend.rag.domain.fusion import (
    apply_intent_freshness,
    fuse,
    merge_hops,
    order_after_rerank,
    rank_lexical,
    rank_vector,
    suppress_duplicates,
    validate_rerank_scores,
)
from backend.rag.domain.paragraph_chunking import (
    DEFAULT_PARAGRAPH_POLICY,
    ParagraphChunkingPolicy,
    paragraph_chunk_bounds,
)
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.query_intent import HopTrace, classify_intent
from backend.rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    ChunkRef,
    CoverageStatus,
    EmptyReason,
    EvidenceContext,
    FilterCoverage,
    FreshnessPolicy,
    FusedCandidate,
    LexicalLimits,
    MetadataProvenance,
    QueryControl,
    RankedCandidate,
    RerankDegradeReason,
    RerankStatus,
    ResultStatus,
    RetrievalAccessDeniedError,
    RetrievalConfigurationError,
    RetrievalError,
    RetrievalMetrics,
    RetrievalMode,
    RetrievalPolicy,
    RetrievalProvenance,
    RetrievalResult,
    RetrievalStorageError,
    RetrievedArticle,
    RetrievedSpan,
    StageMetric,
    VectorBranchStatus,
    build_span,
)
from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    RetrievalTuning,
    SpanKey,
    select_spans,
)

POLICY_RESOURCE = "retrieval-policy.v3.json"


def bundled_retrieval_policy() -> RetrievalPolicy:
    """Load the frozen policy shipped with the package."""
    text = (
        files("backend.rag.application")
        .joinpath("policies")
        .joinpath(POLICY_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return RetrievalPolicy.model_validate_json(text)


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """Per-call switches; defaults are the production hybrid path.

    Disabling a branch is for measurement and explicit fallbacks only and is
    always visible in ``retrieval_mode``/``vector_branch_status``.
    ``remaining_context_budget`` lets T6 pass a smaller remaining budget; it
    can only lower the query budget, never raise it.
    """

    lexical_enabled: bool = True
    vector_enabled: bool = True
    rerank_enabled: bool = True
    remaining_context_budget: int | None = None
    article_scope: ArticleRestriction | None = None


@dataclass(slots=True)
class _Run:
    """Mutable per-query bookkeeping (counts and stage timings only)."""

    timer: Callable[[], float]
    stages: list[StageMetric] = field(default_factory=list[StageMetric])
    backend: str = "postgres"
    index_generation: str | None = None
    fallback_reason: str | None = None
    source_chunker: str | None = None
    source_embedding: str | None = None
    intent: str | None = None
    hops: tuple[HopTrace, ...] = ()
    storage_calls: int = 0
    embedding_calls: int = 0
    rerank_calls: int = 0
    suppressed: int = 0
    dropped: int = 0
    lexical_truncated: int = 0
    branch_cap_dropped: int = 0
    backfill_calls: int = 0
    overlap_dropped: int = 0
    cap_dropped: int = 0
    freshness: str = "policy_mode"
    freshness_boosted: int = 0
    freshness_max_promotion: int = 0
    fill_ids: frozenset[UUID] = frozenset()
    fill_selected: int = 0

    def record(self, stage: str, started: float, inputs: int, outputs: int) -> None:
        latency = max(0.0, (self.timer() - started) * 1000.0)
        self.stages.append(StageMetric(stage, latency, inputs, outputs))

    def metrics(self) -> RetrievalMetrics:
        return RetrievalMetrics(
            stages=tuple(self.stages),
            storage_calls=self.storage_calls,
            embedding_calls=self.embedding_calls,
            rerank_calls=self.rerank_calls,
            suppressed_duplicates=self.suppressed,
            dropped_at_final_check=self.dropped,
            lexical_truncated_articles=self.lexical_truncated,
            branch_cap_dropped=self.branch_cap_dropped,
            backfill_calls=self.backfill_calls,
            selection_overlap_dropped=self.overlap_dropped,
            selection_cap_dropped=self.cap_dropped,
            freshness=self.freshness,
            freshness_boosted=self.freshness_boosted,
            freshness_max_promotion=self.freshness_max_promotion,
            fill_spans_sent=self.fill_selected,
        )


@dataclass(frozen=True, slots=True)
class _Call:
    """Everything one query needs, so steps take a single argument."""

    query: BriefingQuery
    caller: CallerScope
    control: QueryControl
    options: RetrievalOptions
    now: datetime
    run: _Run
    scope: RetrievalScope


@dataclass(slots=True)
class _Slot[T]:
    value: T | None = None
    failed: bool = False
    done: bool = False


@dataclass(frozen=True, slots=True)
class _State:
    """Outcome of the pre-rerank stages."""

    coverage: FilterCoverage
    mode: RetrievalMode
    vector_status: VectorBranchStatus


def call_with_slice[T](
    operation: Callable[[], T], seconds: float, control: QueryControl
) -> _Slot[T]:
    """Run ``operation`` with its own time slice, woken early by cancellation.

    The in-flight call may still finish in its thread, but its result is only
    used when it completed inside the slice and the query is still admitted.
    """
    wake = Event()
    slot: _Slot[T] = _Slot()

    def target() -> None:
        try:
            slot.value = operation()
        except Exception:  # noqa: BLE001 - adapter failures degrade, never leak
            slot.failed = True
        finally:
            slot.done = True
            wake.set()

    control.on_cancel(wake.set)
    Thread(target=target, name="eventlens-rerank", daemon=True).start()
    _ = wake.wait(timeout=max(seconds, 0.0))
    return slot


@dataclass(frozen=True, slots=True)
class ArticleGroups:
    """Selected spans per article (best-chunk order) and what was dropped."""

    groups: tuple[tuple[UUID, tuple[RankedCandidate, ...]], ...]
    overlap_dropped: int
    cap_dropped: int
    fill_chunk_ids: frozenset[UUID] = frozenset()


def _validated_scores(expected: list[UUID], output: object) -> dict[UUID, float] | None:
    """Validate an untrusted reranker result; any malformed shape is invalid."""
    if not isinstance(output, RerankOutput):
        return None
    try:
        return validate_rerank_scores(expected, cast("object", output.scores))
    except Exception:  # noqa: BLE001 - hostile adapter objects degrade only
        return None


def _empty_coverage() -> FilterCoverage:
    return FilterCoverage(
        status=CoverageStatus.COMPLETE,
        metadata_filter_requested=False,
        scoped_articles=0,
        verifiable_articles=0,
        unverifiable_articles=0,
        matching_articles=0,
        scan_cap_reached=False,
    )


def _mode(
    lexical: tuple[BranchCandidate, ...], vector: tuple[BranchCandidate, ...]
) -> RetrievalMode:
    if lexical and vector:
        return RetrievalMode.HYBRID
    if lexical:
        return RetrievalMode.LEXICAL_ONLY
    if vector:
        return RetrievalMode.VECTOR_ONLY
    return RetrievalMode.NONE


def _basis(branches: set[Branch]) -> RetrievalMode:
    if branches == {Branch.LEXICAL, Branch.VECTOR}:
        return RetrievalMode.HYBRID
    if branches == {Branch.VECTOR}:
        return RetrievalMode.VECTOR_ONLY
    return RetrievalMode.LEXICAL_ONLY


@dataclass(frozen=True, slots=True)
class RetrievalDraft:
    """Retrieval between graph steps; ``result`` set means already terminal.

    Carries only values (query, caller, control, candidates, per-query
    counters); never a session, adapter or client.
    """

    call: _Call
    coverage: FilterCoverage = field(default_factory=_empty_coverage)
    state: _State | None = None
    candidates: tuple[FusedCandidate, ...] = ()
    ranked: tuple[RankedCandidate, ...] | None = None
    rerank: tuple[RerankStatus, RerankDegradeReason | None] = (
        RerankStatus.SKIPPED,
        None,
    )
    term_count: int | None = None
    result: RetrievalResult | None = None

    @classmethod
    def finished(cls, call: _Call, result: RetrievalResult) -> "RetrievalDraft":
        """Terminal draft; later steps pass it through unchanged."""
        return cls(call=call, result=result)

    @property
    def candidate_count(self) -> int:
        """Fused candidates so far (0 for terminal drafts)."""
        return len(self.candidates)

    @property
    def control(self) -> QueryControl:
        """The query's cancellation/deadline control."""
        return self.call.control

    def anchor_texts(self, limit: int) -> tuple[str, ...]:
        """Leading text of the top candidates (hop-2 expansion anchors)."""
        return tuple(item.text[:200] for item in self.candidates[:limit])

    def empty_reason(self, state: _State) -> EmptyReason:
        """Why a draft without candidates is empty (same rules as T5)."""
        reason = (
            EmptyReason.NO_QUERY_TERMS
            if self.term_count == 0
            and state.vector_status is not VectorBranchStatus.EXECUTED
            else EmptyReason.NO_MATCHES
        )
        if (
            reason is EmptyReason.NO_MATCHES
            and state.coverage.status is CoverageStatus.FILTER_UNAVAILABLE
        ):
            return EmptyReason.FILTER_UNAVAILABLE
        return reason


def _in_scope(document: ArticleDocument, scope: RetrievalScope) -> bool:
    if scope.source_domains and document.source_domain not in scope.source_domains:
        return False
    published = document.source_published_at
    if scope.published_after is not None and (
        published is None or published < scope.published_after
    ):
        return False
    return scope.published_before is None or (
        published is not None and published < scope.published_before
    )


def _restrict(
    allowed: tuple[UUID, ...] | None, restriction: ArticleRestriction | None
) -> tuple[UUID, ...] | None:
    """Intersect metadata-eligible IDs with the server-resolved article scope."""
    if restriction is None:
        return allowed
    scoped = restriction.article_ids
    keep = scoped if allowed is None else scoped & set(allowed)
    return tuple(sorted(keep, key=str))


def _metadata_matches(state: CurrentArticleState, query: BriefingQuery) -> bool:
    """Topic any-of AND entity any-of over one valid READY publication."""
    metadata = state.metadata
    if metadata is None:
        return False
    if query.topic_filters and not set(query.topic_filters) & set(metadata.topics):
        return False
    wanted = set(query.entity_ids)
    return not wanted or any(entity.entity_id in wanted for entity in metadata.entities)


@dataclass(frozen=True, slots=True)
class RetrieveEvidence:
    """Application service; every dependency is an injected port."""

    store: RetrievalStore
    embeddings: EmbeddingPort | None
    embedding_identity: EmbeddingIdentity
    reranker: RerankerPort | None
    tokens: TokenCounter
    access: AccessPolicy
    policy: RetrievalPolicy
    clock: Callable[[], datetime]
    timer: Callable[[], float] = time.perf_counter
    chunking: ChunkingPolicy = DEFAULT_POLICY
    candidates: CandidateSource | None = None
    fallback_to_store: bool = True
    paragraphs: ParagraphChunkingPolicy = DEFAULT_PARAGRAPH_POLICY
    tuning: RetrievalTuning = BASELINE_TUNING

    @property
    def lexical_on(self) -> bool:
        """Lexical branch weight > 0 (a zero weight disables the branch)."""
        return self.tuning.weights.lexical_enabled

    @property
    def semantic_on(self) -> bool:
        """Semantic branch weight > 0 (zero: no query embedding at all)."""
        return self.tuning.weights.semantic_enabled

    def _branch_freshness(self) -> FreshnessPolicy:
        """v3 branch bonus, or none when intent freshness replaces it."""
        if self.tuning.freshness.mode == "intent":
            return self.policy.freshness.model_copy(update={"weight": 0.0})
        return self.policy.freshness

    def _check_configuration(self, options: RetrievalOptions) -> None:
        if self.policy.chunker_version != self.chunking.version:
            message = "retrieval policy chunker differs from the chunking policy"
            raise RetrievalConfigurationError(message)
        if self.policy.normalization_version != "nfc.v1":
            message = "retrieval policy normalization must be nfc.v1"
            raise RetrievalConfigurationError(message)
        remaining = options.remaining_context_budget
        if remaining is not None and remaining < 0:
            message = "remaining context budget must not be negative"
            raise RetrievalConfigurationError(message)

    def execute(
        self,
        query: BriefingQuery,
        caller: CallerScope,
        control: QueryControl,
        options: RetrievalOptions | None = None,
    ) -> RetrievalResult:
        """Run the full pipeline or raise a typed ``RetrievalError``."""
        draft = self.retrieve_primary(
            self.apply_filters(query, caller, control, options)
        )
        return self.assemble(self.fuse(draft))

    def apply_filters(
        self,
        query: BriefingQuery,
        caller: CallerScope,
        control: QueryControl,
        options: RetrievalOptions | None = None,
    ) -> RetrievalDraft:
        """Access/configuration check, publication range and metadata filters."""
        options = options or RetrievalOptions()
        control.admit()
        run = _Run(self.timer)
        started = self.timer()
        if not self.access.allows(caller):
            raise RetrievalAccessDeniedError
        self._check_configuration(options)
        call = _Call(
            query=query,
            caller=caller,
            control=control,
            options=options,
            now=self.clock(),
            run=run,
            scope=RetrievalScope(
                source_domains=query.source_domains,
                published_after=query.published_after,
                published_before=query.published_before,
                allowed_article_ids=None,
            ),
        )
        run.record("validate", started, 1, 1)
        if (
            query.published_after is not None
            and query.published_after == query.published_before
        ):
            return RetrievalDraft.finished(
                call, self._empty(call, EmptyReason.EMPTY_PUBLICATION_RANGE, None)
            )
        coverage, allowed = self._metadata(call)
        allowed = _restrict(allowed, options.article_scope)
        call = replace(call, scope=replace(call.scope, allowed_article_ids=allowed))
        if allowed is not None and not allowed:
            reason = (
                EmptyReason.FILTER_UNAVAILABLE
                if coverage.status is CoverageStatus.FILTER_UNAVAILABLE
                else EmptyReason.NO_MATCHES
            )
            return RetrievalDraft.finished(
                call,
                self._empty(
                    call,
                    reason,
                    _State(coverage, RetrievalMode.NONE, VectorBranchStatus.NOT_NEEDED),
                ),
            )
        return RetrievalDraft(call=call, coverage=coverage)

    def retrieve_primary(self, draft: RetrievalDraft) -> RetrievalDraft:
        """Hop 1: both branches on the user query, RRF and dedup."""
        if draft.result is not None:
            return draft
        return self._gather(draft, draft.call.query.query, hop=1)

    def retrieve_related(self, draft: RetrievalDraft, text: str) -> RetrievalDraft:
        """Bounded hop 2: same scope and filters, one expanded query text."""
        if draft.result is not None:
            return draft
        return self._gather(draft, text, hop=2)

    def fuse(
        self,
        primary: RetrievalDraft,
        related: RetrievalDraft | None = None,
        *,
        intent: str | None = None,
        hops: tuple[HopTrace, ...] = (),
    ) -> RetrievalDraft:
        """Merge hop results (RRF over hop ranks), then the single bounded rerank."""
        call = primary.call
        call.run.intent = intent
        call.run.hops = hops
        if primary.result is not None:
            return primary
        state = primary.state
        assert state is not None  # noqa: S101 - set by _gather
        merged = primary.candidates
        if related is not None and related.result is None and related.candidates:
            started = call.run.timer()
            merged = merge_hops(
                primary.candidates, related.candidates, self.policy.rrf.k
            )
            merged, suppressed = suppress_duplicates(merged, self.policy.dedup)
            call.run.suppressed += suppressed
            call.run.record(
                "hop_fusion",
                started,
                len(primary.candidates) + len(related.candidates),
                len(merged),
            )
        call.control.admit()
        if not merged:
            return RetrievalDraft.finished(
                call, self._empty(call, primary.empty_reason(state), state)
            )
        ranked, status, degrade = self._rerank(call, merged)
        call.control.admit()
        return replace(
            primary, candidates=merged, ranked=ranked, rerank=(status, degrade)
        )

    def assemble(self, draft: RetrievalDraft) -> RetrievalResult:
        """Final current/tombstone/scope check and token-budgeted evidence."""
        if draft.result is not None:
            return draft.result
        if draft.state is None or draft.ranked is None:
            message = "assemble requires a fused draft"
            raise RetrievalConfigurationError(message)
        status, degrade = draft.rerank
        return self._finish(draft.call, draft.state, draft.ranked, status, degrade)

    def _gather(self, draft: RetrievalDraft, text: str, *, hop: int) -> RetrievalDraft:
        call = draft.call
        coverage = draft.coverage
        lexical, vector, vector_status, term_count = self._branches(call, text)
        state = _State(coverage, _mode(lexical, vector), vector_status)
        call.control.admit()
        started = call.run.timer()
        fused = fuse(lexical, vector, self.policy.rrf.k, self.tuning.weights)
        stage = "fusion" if hop == 1 else f"fusion_hop{hop}"
        call.run.record(stage, started, len(lexical) + len(vector), len(fused))
        started = call.run.timer()
        deduped, suppressed = suppress_duplicates(fused, self.policy.dedup)
        call.run.suppressed += suppressed
        call.run.record(
            "dedup" if hop == 1 else f"dedup_hop{hop}",
            started,
            len(fused),
            len(deduped),
        )
        if hop == 1 and self.tuning.freshness.mode == "intent":
            # After dedup on purpose: dedup re-sorts by fused score, so the
            # rank guard must be the last reordering of the hop-1 list. The
            # baseline is the deduplicated, unboosted order; a representative
            # is boosted by its own (shown) publication time.
            started = call.run.timer()
            deduped, decision = apply_intent_freshness(
                deduped,
                call.now,
                self.tuning.freshness,
                classify_intent(call.query.query).intent.value,
                self.policy.rrf.k,
                explicit_window=call.query.published_after is not None
                or call.query.published_before is not None,
                weight_sum=(self.tuning.weights.lexical if lexical else 0.0)
                + (self.tuning.weights.semantic if vector else 0.0),
            )
            call.run.freshness = decision.reason
            call.run.freshness_boosted = decision.boosted
            call.run.freshness_max_promotion = decision.max_promotion
            call.run.record("freshness", started, len(deduped), decision.boosted)
        call.control.admit()
        if hop > 1:
            return replace(draft, candidates=deduped)
        return replace(draft, state=state, candidates=deduped, term_count=term_count)

    def _branches(
        self, call: _Call, text: str
    ) -> tuple[
        tuple[BranchCandidate, ...],
        tuple[BranchCandidate, ...],
        VectorBranchStatus,
        int | None,
    ]:
        """Index-backed candidates when configured, else (or on outage) PostgreSQL."""
        source = self.candidates
        if source is not None:
            call.control.admit()
            started = call.run.timer()
            try:
                batch = source.candidates(
                    text,
                    call.scope,
                    call.query,
                    call.caller,
                    call.control,
                    lexical=call.options.lexical_enabled and self.lexical_on,
                    semantic=call.options.vector_enabled and self.semantic_on,
                    tuning=self.tuning,
                )
            except CandidateSourceUnavailableError as error:
                call.control.admit()
                if not self.fallback_to_store:
                    raise RetrievalStorageError from error
                call.run.backend = "postgres_fallback"
                call.run.fallback_reason = error.reason
                call.run.record("search_index", started, 1, 0)
            else:
                call.control.admit()
                call.run.backend = batch.backend
                call.run.index_generation = batch.generation
                call.run.source_chunker = batch.chunker_version
                call.run.source_embedding = batch.embedding_identity
                call.run.storage_calls += batch.storage_calls
                call.run.embedding_calls += batch.embedding_calls
                call.run.branch_cap_dropped += batch.branch_cap_dropped
                call.run.backfill_calls += batch.backfill_calls
                call.run.record(
                    "search_index",
                    started,
                    1,
                    len(batch.lexical) + len(batch.vector),
                )
                return (
                    batch.lexical,
                    batch.vector,
                    batch.vector_status,
                    batch.term_count,
                )
        lexical, term_count = self._lexical(call, text)
        vector, vector_status = self._vector(call, text)
        return lexical, vector, vector_status, term_count

    def _metadata(self, call: _Call) -> tuple[FilterCoverage, tuple[UUID, ...] | None]:
        query = call.query
        if not query.topic_filters and not query.entity_ids:
            return _empty_coverage(), None
        call.control.admit()
        started = call.run.timer()
        call.run.storage_calls += 1
        found = self.store.metadata_eligibility(
            call.scope,
            query.topic_filters,
            query.entity_ids,
            self.policy.filters.metadata_scan_cap,
        )
        call.control.admit()
        unverifiable = max(0, found.scoped_articles - found.verifiable_articles)
        if found.verifiable_articles == 0 and unverifiable > 0:
            status = CoverageStatus.FILTER_UNAVAILABLE
        elif unverifiable > 0 or found.scan_cap_reached:
            status = CoverageStatus.PARTIAL
        else:
            status = CoverageStatus.COMPLETE
        matching = found.matching_article_ids
        call.run.record(
            "metadata_filter", started, found.scoped_articles, len(matching)
        )
        return (
            FilterCoverage(
                status=status,
                metadata_filter_requested=True,
                scoped_articles=found.scoped_articles,
                verifiable_articles=found.verifiable_articles,
                unverifiable_articles=unverifiable,
                matching_articles=len(matching),
                scan_cap_reached=found.scan_cap_reached,
            ),
            matching,
        )

    def _lexical(
        self, call: _Call, text: str
    ) -> tuple[tuple[BranchCandidate, ...], int | None]:
        """Bounded lexical branch; returns candidates and the query lexeme count."""
        if not call.options.lexical_enabled or not self.lexical_on:
            return (), None
        call.control.admit()
        started = call.run.timer()
        call.run.storage_calls += 1
        lexical = self.policy.lexical
        page = self.store.lexical_chunks(
            text,
            call.scope,
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
        call.control.admit()
        if len(page.chunks) > lexical.max_metadata_candidates:
            # v3: a breached metadata bound is a typed failure, never a silent cut
            # that could drop part of a tie group before the chunk_id tie-break.
            message = "lexical metadata candidates exceed the frozen v3 bound"
            raise RetrievalConfigurationError(message)
        call.run.lexical_truncated = page.truncated_articles
        ranked = rank_lexical(
            page,
            call.now,
            lexical,
            self._branch_freshness(),
            self.policy.chunker_version,
        )
        ranked = ranked[: lexical.max_text_chunks]
        if ranked:
            # Text only for the <= max_text_chunks selected by the frozen key.
            call.control.admit()
            call.run.storage_calls += 1
            texts = self.store.chunk_texts(tuple(item.chunk for item in ranked))
            call.control.admit()
            ranked = tuple(
                replace(item, text=text)
                for item, text in zip(ranked, texts, strict=True)
            )
        call.run.record("lexical", started, len(page.chunks), len(ranked))
        return ranked, page.term_count

    def _compatible(self, vector: EmbeddingVector) -> bool:
        identity = self.embedding_identity
        return (
            vector.model == identity.model
            and vector.version == identity.version
            and len(vector.values) == identity.dimension
            and identity.chunker_version == self.policy.chunker_version
            and identity.normalization_version == self.policy.normalization_version
            and all(math.isfinite(value) for value in vector.values)
            and any(value != 0.0 for value in vector.values)
        )

    def _vector(
        self, call: _Call, text: str
    ) -> tuple[tuple[BranchCandidate, ...], VectorBranchStatus]:
        if (
            not call.options.vector_enabled
            or not self.semantic_on
            or self.embeddings is None
        ):
            return (), VectorBranchStatus.DISABLED
        call.control.admit()
        started = call.run.timer()
        call.run.embedding_calls += 1
        try:
            vector = self.embeddings.embed(normalize("NFC", text))
        except RetrievalError:
            raise
        except Exception:  # noqa: BLE001 - provider failure means lexical_only
            call.control.admit()
            call.run.record("embedding", started, 1, 0)
            return (), VectorBranchStatus.EMBEDDING_FAILED
        call.control.admit()
        call.run.record("embedding", started, 1, 1)
        if not self._compatible(vector):
            return (), VectorBranchStatus.EMBEDDING_INCOMPATIBLE
        started = call.run.timer()
        call.run.storage_calls += 1
        hits = self.store.vector_chunks(
            vector.values, self.embedding_identity, call.scope, self.policy.vector.pool
        )
        call.control.admit()
        ranked = rank_vector(
            hits,
            call.now,
            self.policy.vector,
            self._branch_freshness(),
            self.policy.chunker_version,
        )
        call.run.record("vector", started, len(hits), len(ranked))
        return ranked, VectorBranchStatus.EXECUTED

    def _rerank(
        self, call: _Call, deduped: tuple[FusedCandidate, ...]
    ) -> tuple[tuple[RankedCandidate, ...], RerankStatus, RerankDegradeReason | None]:
        limits = self.policy.rerank
        window = deduped[: limits.max_candidates]
        tail = deduped[limits.max_candidates :]
        started = call.run.timer()
        reranker = self.reranker
        if not call.options.rerank_enabled or reranker is None:
            ranked = order_after_rerank(window, tail, None)
            call.run.record("rerank", started, len(window), 0)
            return ranked, RerankStatus.SKIPPED, None
        candidates = tuple(
            RerankCandidate(
                candidate_id=item.chunk.chunk_id,
                text=item.text[
                    : self.tokens.prefix_end(item.text, limits.max_tokens_per_candidate)
                ],
            )
            for item in window
        )
        call.control.admit()
        seconds = min(limits.timeout_seconds, call.control.remaining())
        request = RerankRequest(call.query.query, candidates, seconds)
        call.run.rerank_calls += 1
        slot: _Slot[object] = call_with_slice(
            lambda: reranker.rerank(request), seconds, call.control
        )
        call.control.admit()
        reason: RerankDegradeReason | None = None
        scores = None
        if not slot.done:
            reason = RerankDegradeReason.TIMEOUT
        elif slot.failed or slot.value is None:
            reason = RerankDegradeReason.ERROR
        else:
            scores = _validated_scores(
                [item.candidate_id for item in candidates], slot.value
            )
            if scores is None:
                reason = RerankDegradeReason.INVALID_OUTPUT
        ranked = order_after_rerank(window, tail, scores)
        call.run.record("rerank", started, len(window), len(window))
        if reason is not None:
            return ranked, RerankStatus.DEGRADED, reason
        return ranked, RerankStatus.APPLIED, None

    def _final_check(
        self,
        call: _Call,
        groups: tuple[tuple[UUID, tuple[RankedCandidate, ...]], ...],
    ) -> list[PreparedArticle]:
        """Batched final current/access/scope/metadata check (one storage call)."""
        call.control.admit()
        if not self.access.allows(call.caller):
            raise RetrievalAccessDeniedError
        started = call.run.timer()
        call.run.storage_calls += 1
        states = {
            state.document.article_id: state
            for state in self.store.current_articles(
                tuple(article_id for article_id, _ in groups)
            )
        }
        call.control.admit()
        # R1: a revocation completed while the final read was in flight must
        # still win; one policy check, independent of the number of articles.
        if not self.access.allows(call.caller):
            raise RetrievalAccessDeniedError
        metadata_requested = bool(call.query.topic_filters or call.query.entity_ids)
        prepared: list[PreparedArticle] = []
        for article_id, items in groups:
            state = states.get(article_id)
            article = (
                None
                if state is None
                else self._prepare(call, state, items, metadata_requested)
            )
            if article is None:
                call.run.dropped += 1
            else:
                prepared.append(article)
        call.run.record("final_check", started, len(groups), len(prepared))
        return prepared

    def _prepare(
        self,
        call: _Call,
        state: CurrentArticleState,
        items: tuple[RankedCandidate, ...],
        metadata_requested: bool,  # noqa: FBT001
    ) -> PreparedArticle | None:
        document = state.document
        if document.content_hash != items[0].candidate.chunk.content_hash:
            return None
        if not _in_scope(document, call.scope):
            return None
        restriction = call.options.article_scope
        if restriction is not None and not restriction.admits(
            document.article_id, document.content_hash
        ):
            return None
        if metadata_requested and not _metadata_matches(state, call.query):
            return None
        # Index-backed candidates were checked against their own generation
        # by the search adapter; PostgreSQL only confirms the current revision.
        indexed = call.run.backend not in {"postgres", "postgres_fallback"}
        spans: list[PreparedSpan] = []
        branches: set[Branch] = set()
        for item in items:
            candidate = item.candidate
            ref = candidate.chunk
            if (
                candidate.lexical_rank is None
                and not indexed
                and candidate.vector_generation != state.active_index_generation
            ):
                continue
            if self._bounds(document, ref) != (ref.start_offset, ref.end_offset):
                continue
            if not document.normalized_content[
                ref.start_offset : ref.end_offset
            ].strip():
                continue
            span = build_span(
                document.article_id,
                document.content_hash,
                document.normalized_content,
                ref.start_offset,
                ref.end_offset,
            )
            current_branches = tuple(
                branch
                for branch in candidate.branches
                if branch is Branch.LEXICAL
                or indexed
                or candidate.vector_generation == state.active_index_generation
            )
            branches.update(current_branches)
            spans.append(
                PreparedSpan(
                    span=span,
                    template=RetrievedSpan(
                        span=span,
                        chunk_id=ref.chunk_id,
                        chunk_index=ref.chunk_index,
                        branches=current_branches,
                        lexical_rank=candidate.lexical_rank,
                        vector_rank=candidate.vector_rank,
                        fused_score=candidate.fused_score,
                        rerank_score=item.rerank_score,
                        truncated=False,
                    ),
                    fill=ref.chunk_id in call.run.fill_ids,
                    priority=item.position,
                )
            )
        if not spans:
            return None
        basis = _basis(branches)
        metadata = state.metadata
        provenance = (
            None
            if metadata is None or state.metadata_generation is None
            else MetadataProvenance(
                enrichment_version=metadata.enrichment_version,
                model_version=metadata.model_version,
                prompt_version=metadata.prompt_version,
                index_generation=state.metadata_generation,
            )
        )
        return PreparedArticle(
            article_id=document.article_id,
            content_hash=document.content_hash,
            normalized_content=document.normalized_content,
            title=document.title,
            source_name=document.source_name,
            source_domain=document.source_domain,
            source_published_at=document.source_published_at,
            retrieval_basis=basis,
            vector_generation=(
                None
                if basis is RetrievalMode.LEXICAL_ONLY or indexed
                else state.active_index_generation
            ),
            metadata=provenance,
            duplicate_article_ids=tuple(
                sorted(
                    {
                        duplicate
                        for item in items
                        for duplicate in item.candidate.duplicate_article_ids
                    },
                    key=str,
                )
            ),
            spans=tuple(spans),
            indexed=indexed,
        )

    def _bounds(
        self, document: ArticleDocument, ref: ChunkRef
    ) -> tuple[int, int] | None:
        """Recompute a chunk's offsets with the chunker that produced it."""
        text = document.normalized_content
        if ref.chunker_version == self.chunking.version:
            return chunk_bounds(len(text), ref.chunk_index, self.chunking)
        if ref.chunker_version == self.paragraphs.version:
            return paragraph_chunk_bounds(text, ref.chunk_index, self.paragraphs)
        return None

    def select(self, ranked: tuple[RankedCandidate, ...]) -> ArticleGroups:
        """Article/passage selection (cap, overlap) in best-chunk order."""
        selection = self.tuning.selection
        cap = (
            selection.max_spans_per_article
            or self.policy.aggregation.max_spans_per_article
        )
        chosen = select_spans(
            tuple(
                SpanKey(
                    article_id=item.candidate.chunk.article_id,
                    content_hash=item.candidate.chunk.content_hash,
                    start=item.candidate.chunk.start_offset,
                    end=item.candidate.chunk.end_offset,
                )
                for item in ranked
            ),
            cap,
            selection.overlap_threshold,
            selection.fill_spans_per_article,
        )
        return ArticleGroups(
            groups=tuple(
                (article_id, tuple(ranked[i] for i in positions))
                for article_id, positions in chosen.groups
            ),
            overlap_dropped=chosen.overlap_dropped,
            cap_dropped=chosen.cap_dropped,
            fill_chunk_ids=frozenset(
                ranked[i].candidate.chunk.chunk_id for i in chosen.fill
            ),
        )

    def _budget(self, call: _Call) -> int:
        budget = min(call.query.context_budget, self.policy.output.max_context_tokens)
        remaining = call.options.remaining_context_budget
        return budget if remaining is None else min(budget, remaining)

    def _finish(
        self,
        call: _Call,
        state: _State,
        ranked: tuple[RankedCandidate, ...],
        status: RerankStatus,
        degrade: RerankDegradeReason | None,
    ) -> RetrievalResult:
        started = call.run.timer()
        article_cap = min(call.query.result_limit, self.policy.output.max_articles)
        groups = self.select(ranked)
        call.run.overlap_dropped += groups.overlap_dropped
        call.run.cap_dropped += groups.cap_dropped
        call.run.fill_ids = groups.fill_chunk_ids
        pool = groups.groups[
            : article_cap * self.policy.aggregation.final_check_pool_multiplier
        ]
        call.run.record("aggregate", started, len(ranked), len(pool))
        prepared = self._final_check(call, pool)[:article_cap]
        if not prepared:
            return self._empty(
                call, EmptyReason.DROPPED_AT_FINAL_CHECK, state, status, degrade
            )
        started = call.run.timer()
        assembled = EvidenceAssembler(
            self.tokens, self.policy.output, self.tuning.selection.packing
        ).assemble(tuple(prepared), self._budget(call))
        call.run.record("assemble", started, len(prepared), len(assembled.articles))
        call.run.fill_selected = sum(
            1
            for article in assembled.articles
            for span in article.spans
            if span.chunk_id in call.run.fill_ids
        )
        call.control.admit()
        if not self.access.allows(call.caller):  # R1: response boundary
            raise RetrievalAccessDeniedError
        if not assembled.articles:
            return self._empty(
                call, EmptyReason.CONTEXT_BUDGET_TOO_SMALL, state, status, degrade
            )
        return RetrievalResult(
            status=ResultStatus.EVIDENCE,
            empty_reason=None,
            retrieval_mode=state.mode,
            vector_branch_status=state.vector_status,
            rerank_status=status,
            rerank_degrade_reason=degrade,
            coverage=state.coverage,
            articles=assembled.articles,
            context=assembled.context,
            provenance=self._provenance(call),
            metrics=call.run.metrics(),
        )

    def _provenance(self, call: _Call) -> RetrievalProvenance:
        identity = self.embedding_identity
        reranker = None if self.reranker is None else self.reranker.identity
        run = call.run
        return RetrievalProvenance(
            search_backend=run.backend,
            index_generation=run.index_generation,
            search_fallback_reason=run.fallback_reason,
            source_chunker_version=run.source_chunker,
            source_embedding_identity=run.source_embedding,
            query_intent=run.intent,
            hops=run.hops,
            policy_id=self.policy.policy_id,
            fusion_policy_id=self.policy.fusion_policy_id,
            index_version=(
                f"{self.policy.policy_id}|{identity.corpus_generation}|"
                f"{identity.model}:{identity.version}|{self.policy.chunker_version}|"
                f"{self.policy.normalization_version}"
            ),
            chunker_version=self.policy.chunker_version,
            normalization_version=self.policy.normalization_version,
            embedding_model=identity.model,
            embedding_version=identity.version,
            corpus_generation=identity.corpus_generation,
            reranker_id=None if reranker is None else reranker.reranker_id,
            reranker_version=None if reranker is None else reranker.version,
            publication_time_basis="source_published_at",
            query_time=call.now,
            tuning_id=self.tuning.tuning_id,
        )

    def _empty(
        self,
        call: _Call,
        reason: EmptyReason,
        state: _State | None,
        status: RerankStatus = RerankStatus.SKIPPED,
        degrade: RerankDegradeReason | None = None,
    ) -> RetrievalResult:
        call.control.admit()
        # R5: every return path, empty ones included, passes the same final
        # access boundary; a denied caller gets no counts or coverage.
        if not self.access.allows(call.caller):
            raise RetrievalAccessDeniedError
        identity = self.tokens.identity
        current = state or _State(
            _empty_coverage(), RetrievalMode.NONE, VectorBranchStatus.NOT_NEEDED
        )
        return RetrievalResult(
            status=ResultStatus.EMPTY,
            empty_reason=reason,
            retrieval_mode=current.mode,
            vector_branch_status=current.vector_status,
            rerank_status=status,
            rerank_degrade_reason=degrade,
            coverage=current.coverage,
            articles=(),
            context=EvidenceContext(
                context_format=self.policy.output.context_format,
                text="",
                token_count=0,
                token_budget=self._budget(call),
                tokenizer_id=identity.tokenizer_id,
                tokenizer_version=identity.version,
                truncated_spans=0,
                omitted_articles=0,
            ),
            provenance=self._provenance(call),
            metrics=call.run.metrics(),
        )


__all__ = [
    "ArticleGroups",
    "RetrievalDraft",
    "RetrievalOptions",
    "RetrieveEvidence",
    "RetrievedArticle",
    "bundled_retrieval_policy",
    "call_with_slice",
]
