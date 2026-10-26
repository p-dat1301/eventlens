"""Hybrid retrieval policy, value types, query control and typed failures.

Everything here is framework-free. Public results (``RetrievalResult`` and the
types it holds) are the contract T6/T7 reuse; raw storage rows never leave the
adapters.
"""

# ruff: noqa: EM101

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from threading import Event, Lock
from typing import Annotated, ClassVar, Literal, LiteralString, Self
from unicodedata import normalize
from uuid import UUID, uuid5

from pydantic import Field, PositiveInt, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import ContractModel, NonEmptyText
from backend.shared.contracts.evidence import EvidenceSpan

CHUNK_NAMESPACE = UUID("5f0c4a8e-7d1b-4c55-9a3e-2b6f1d9c0e11")
SPAN_NAMESPACE = UUID("9b2e6d41-3c7a-4f08-8e15-6a4d2c1b0f22")
MAX_BRANCH_DEPTH = 50
MAX_RERANK_CANDIDATES = 40
MAX_RERANK_TOKENS = 512
MAX_ARTICLES = 20
MAX_CONTEXT_TOKENS = 12_000
_WORD = re.compile(r"\w+")
_SPACE = re.compile(r"\s+")


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


LEXICAL_TIES = (
    "score_desc",
    "document_rank_desc",
    "source_published_at_desc_nulls_last",
    "article_id_asc",
    "chunk_id_asc",
)
VECTOR_TIES = (
    "score_desc",
    "source_published_at_desc_nulls_last",
    "article_id_asc",
    "chunk_id_asc",
)
FUSED_TIES = (
    "fused_score_desc",
    "source_published_at_desc_nulls_last",
    "article_id_asc",
    "chunk_id_asc",
)
RERANKED_TIES = (
    "rerank_score_desc",
    "fused_score_desc",
    "source_published_at_desc_nulls_last",
    "article_id_asc",
    "chunk_id_asc",
)


class TokenizerPolicy(ContractModel):
    """Query/chunk term tokenizer shared by lexical scoring and dedup."""

    tokenizer_id: Literal["postgres-simple-lexemes.v2"]
    source: Literal["to_tsvector_simple_of_nfc_query"]
    chunk_terms: Literal["to_tsvector_simple_of_title_plus_chunk"]
    term_order: Literal["first_position_then_lexeme"]
    max_query_terms: Annotated[int, Field(ge=1, le=64)]
    fts_config: Literal["simple"]
    tsquery_operator: Literal["or"]
    stopwords: tuple[NonEmptyText, ...]


class FilterPolicy(ContractModel):
    """Frozen filter order and boundary semantics."""

    order: tuple[NonEmptyText, ...]
    publication_field: Literal["source_published_at"]
    published_after: Literal["inclusive"]
    published_before: Literal["exclusive"]
    equal_bounds: Literal["empty_range"]
    null_publication_time: Literal["excluded_when_any_bound_set"]
    topic_semantics: Literal["any_of_requested_topics"]
    entity_semantics: Literal["any_of_requested_entity_ids"]
    across_filter_kinds: Literal["all_of"]
    metadata_source: Literal[
        "valid_ready_publication_for_current_revision_and_active_generation"
    ]
    metadata_scan_cap: PositiveInt


class LexicalPolicy(ContractModel):
    """Lexical FTS branch caps."""

    article_pool: Annotated[int, Field(ge=1, le=200)]
    max_chunks_scanned_per_article: Annotated[int, Field(ge=1, le=200)]
    max_chunks_per_article: PositiveInt
    max_metadata_candidates: Annotated[int, Field(ge=1, le=10_000)]
    max_text_chunks: Annotated[int, Field(ge=1, le=MAX_BRANCH_DEPTH)]
    depth: Annotated[int, Field(ge=1, le=MAX_BRANCH_DEPTH)]
    selection_stages: tuple[NonEmptyText, ...]
    tie_boundary: Literal["keep_all_windows_tied_at_per_article_cut_superset"]
    metadata_bound_exceeded: Literal["typed_configuration_error_never_silent_cut"]
    scan_limit_exceeded: Literal["report_lexical_truncated_articles_never_widen"]
    chunk_score: Literal["distinct_query_lexemes_in_title_plus_chunk_div_query_lexemes"]
    ties: NonEmptyText


class VectorPolicy(ContractModel):
    """Vector branch caps and route; ANN stays disabled in v1."""

    route: Literal["exact"]
    pool: PositiveInt
    depth: Annotated[int, Field(ge=1, le=MAX_BRANCH_DEPTH)]
    max_chunks_per_article: PositiveInt
    chunk_score: Literal["cosine_similarity"]
    ties: NonEmptyText
    ann_enabled: Literal[False]
    ann_note: NonEmptyText


class FreshnessPolicy(ContractModel):
    """Additive exponential freshness bonus applied inside each branch."""

    function: Literal["branch_score + weight * 0.5 ** (age_days / half_life_days)"]
    half_life_days: Annotated[float, Field(gt=0)]
    weight: Annotated[float, Field(ge=0, le=1)]
    missing_timestamp: Literal["no_boost"]
    future_timestamp: Literal["no_boost"]
    applies_to: Literal["branch_score_before_branch_rank_assignment"]


class RrfPolicy(ContractModel):
    """Reciprocal rank fusion constants (D35/D43)."""

    k: Literal[60]
    formula: Literal["sum(1 / (k + rank_b))"]
    missing_branch_contribution: Literal[0]
    ties: NonEmptyText


class DedupPolicy(ContractModel):
    """Exact and near-duplicate suppression before rerank."""

    stage: Literal["after_fusion_before_rerank"]
    exact_fingerprint: Literal["sha256(casefold(nfc(text)) with whitespace collapsed)"]
    near_duplicate: Literal["word_shingle_jaccard"]
    shingle_size: Annotated[int, Field(ge=1, le=8)]
    near_duplicate_threshold: Annotated[float, Field(gt=0, le=1)]
    near_duplicate_scope: Literal["cross_article"]
    representative: Literal[
        "earliest_source_published_at_nulls_last,article_id_asc,chunk_id_asc"
    ]
    cluster_score: Literal["max_member_fused_score"]
    note: NonEmptyText


class RerankPolicy(ContractModel):
    """Bounded rerank window and invalid-output rules."""

    max_candidates: Annotated[int, Field(ge=1, le=MAX_RERANK_CANDIDATES)]
    max_tokens_per_candidate: Annotated[int, Field(ge=1, le=MAX_RERANK_TOKENS)]
    timeout_seconds: Annotated[float, Field(gt=0, le=1.5)]
    beyond_window: Literal["appended_in_fused_order"]
    missing_ids: Literal["invalid"]
    unknown_ids: Literal["invalid"]
    duplicate_ids: Literal["invalid"]
    non_finite_scores: Literal["invalid"]
    malformed_output: Literal["invalid"]
    ties: NonEmptyText


class AggregationPolicy(ContractModel):
    """Chunk-to-article aggregation after rerank."""

    article_order: Literal["best_chunk_position_after_rerank"]
    max_spans_per_article: Annotated[int, Field(ge=1, le=10)]
    span_order: Literal["reranked_order"]
    final_check_pool_multiplier: Annotated[int, Field(ge=1, le=4)]
    final_check_offsets: Literal["chunk_arithmetic_no_rechunking"]


class OutputPolicy(ContractModel):
    """Article and context-token caps applied after aggregation."""

    max_articles: Annotated[int, Field(ge=1, le=MAX_ARTICLES)]
    max_context_tokens: Annotated[int, Field(ge=1, le=MAX_CONTEXT_TOKENS)]
    min_truncated_span_tokens: PositiveInt
    context_format: Literal["evidence-context-jsonl.v1"]
    budget_exhausted: Literal["stop_at_first_span_that_does_not_fit_after_truncation"]


class RetrievalPolicy(ContractModel):
    """Versioned D35/D43 retrieval policy; any change needs a new version."""

    schema_version: Literal["retrieval_policy.v3"]
    policy_id: NonEmptyText
    supersedes: NonEmptyText
    change_reason: NonEmptyText
    fusion_policy_id: NonEmptyText
    rationale: NonEmptyText
    query_clock: Literal["injected_wall_clock_read_once_per_query"]
    chunker_version: NonEmptyText
    normalization_version: NonEmptyText
    tokenizer: TokenizerPolicy
    filters: FilterPolicy
    lexical: LexicalPolicy
    vector: VectorPolicy
    freshness: FreshnessPolicy
    rrf: RrfPolicy
    dedup: DedupPolicy
    rerank: RerankPolicy
    aggregation: AggregationPolicy
    output: OutputPolicy

    @model_validator(mode="after")
    def enforce_pool_depth(self) -> Self:
        """Database pools must be able to fill each branch depth."""
        if self.vector.pool < self.vector.depth:
            raise _error("vector_pool_too_small", "vector pool must cover depth")
        lexical = self.lexical
        if (
            lexical.max_metadata_candidates
            != lexical.article_pool * lexical.max_chunks_scanned_per_article
        ):
            raise _error(
                "metadata_bound_mismatch",
                "max_metadata_candidates must equal article_pool x scan cap",
            )
        if lexical.max_text_chunks < lexical.depth:
            raise _error("text_bound_below_depth", "max_text_chunks must cover depth")
        frozen = (
            (self.lexical.ties, LEXICAL_TIES),
            (self.vector.ties, VECTOR_TIES),
            (self.rrf.ties, FUSED_TIES),
            (self.rerank.ties, RERANKED_TIES),
        )
        if any(
            tuple(declared.split(",")) != implemented
            for declared, implemented in frozen
        ):
            raise _error(
                "tie_break_mismatch", "policy tie-breaks must match the implementation"
            )
        return self


class Branch(StrEnum):
    """Candidate-producing retrieval branch."""

    LEXICAL = "lexical"
    VECTOR = "vector"


class RetrievalMode(StrEnum):
    """Which branches contributed candidates for the whole query."""

    HYBRID = "hybrid"
    LEXICAL_ONLY = "lexical_only"
    VECTOR_ONLY = "vector_only"
    NONE = "none"


class VectorBranchStatus(StrEnum):
    """Why the semantic branch did or did not contribute."""

    EXECUTED = "executed"
    DISABLED = "disabled"
    EMBEDDING_INCOMPATIBLE = "embedding_incompatible"
    EMBEDDING_FAILED = "embedding_failed"
    NOT_NEEDED = "not_needed"


class RerankStatus(StrEnum):
    """Rerank outcome; anything but APPLIED keeps fused order."""

    APPLIED = "applied"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


class RerankDegradeReason(StrEnum):
    """Stable reason when rerank degraded to fused order."""

    TIMEOUT = "timeout"
    ERROR = "error"
    INVALID_OUTPUT = "invalid_output"


class CoverageStatus(StrEnum):
    """Metadata-filter coverage over the filtered corpus."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FILTER_UNAVAILABLE = "filter_unavailable"


class ResultStatus(StrEnum):
    """Whether evidence was produced."""

    EVIDENCE = "evidence"
    EMPTY = "empty"


class EmptyReason(StrEnum):
    """Distinguishable empty outcomes (never used for failures)."""

    NO_QUERY_TERMS = "no_query_terms"
    NO_MATCHES = "no_matches"
    EMPTY_PUBLICATION_RANGE = "empty_publication_range"
    FILTER_UNAVAILABLE = "filter_unavailable"
    DROPPED_AT_FINAL_CHECK = "dropped_at_final_check"
    CONTEXT_BUDGET_TOO_SMALL = "context_budget_too_small"


class RetrievalError(RuntimeError):
    """Base typed retrieval failure; never an empty result."""

    code: ClassVar[str] = "retrieval_failed"


class RetrievalCancelledError(RetrievalError):
    """Shared cancellation was requested; the query stops."""

    code: ClassVar[str] = "retrieval_cancelled"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("retrieval cancelled")


class RetrievalDeadlineExceededError(RetrievalError):
    """End-to-end deadline elapsed; the query stops."""

    code: ClassVar[str] = "retrieval_deadline_exceeded"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("retrieval deadline exceeded")


class RetrievalAccessDeniedError(RetrievalError):
    """Trusted caller scope is not (or no longer) authorized."""

    code: ClassVar[str] = "retrieval_access_denied"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("retrieval access denied")


class RetrievalStorageError(RetrievalError):
    """Index storage unavailable or failed; not an empty result."""

    code: ClassVar[str] = "retrieval_storage_unavailable"

    def __init__(self) -> None:
        """Hide connection details."""
        super().__init__("retrieval storage unavailable")


class RetrievalConfigurationError(RetrievalError):
    """Policy, chunker or budget configuration is inconsistent."""

    code: ClassVar[str] = "retrieval_configuration_invalid"


class QueryControl:
    """Shared cancellation plus end-to-end deadline for one query.

    ``admit`` is called before every storage, embedding and rerank operation and
    before each subsequent step; it raises the typed error and never lets a
    cancelled or expired run start more work. Waiters registered with
    ``on_cancel`` are woken when ``cancel`` is called.
    """

    def __init__(
        self,
        deadline_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Fix the absolute deadline at construction."""
        self._monotonic: Callable[[], float] = monotonic
        self._deadline: float = monotonic() + deadline_seconds
        self._cancelled: Event = Event()
        self._lock: Lock = Lock()
        self._waiters: list[Callable[[], None]] = []

    def cancel(self) -> None:
        """Request cancellation; wake every registered waiter."""
        with self._lock:
            self._cancelled.set()
            waiters = tuple(self._waiters)
        for waiter in waiters:
            waiter()

    def on_cancel(self, waiter: Callable[[], None]) -> None:
        """Register a wake-up callback (called immediately if already cancelled)."""
        with self._lock:
            self._waiters.append(waiter)
            cancelled = self._cancelled.is_set()
        if cancelled:
            waiter()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation was requested."""
        return self._cancelled.is_set()

    def remaining(self) -> float:
        """Seconds left before the end-to-end deadline (may be negative)."""
        return self._deadline - self._monotonic()

    def admit(self) -> None:
        """Raise if no further work may start."""
        if self._cancelled.is_set():
            raise RetrievalCancelledError
        if self.remaining() <= 0:
            raise RetrievalDeadlineExceededError


@dataclass(frozen=True, slots=True)
class ChunkRef:
    """Deterministic chunk identity shared by lexical and vector branches."""

    article_id: UUID
    content_hash: str
    chunker_version: str
    chunk_index: int
    start_offset: int
    end_offset: int

    @property
    def chunk_id(self) -> UUID:
        """Stable ID independent of storage row IDs and index generation."""
        return uuid5(
            CHUNK_NAMESPACE,
            "|".join(
                (
                    str(self.article_id),
                    self.content_hash,
                    self.chunker_version,
                    str(self.chunk_index),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class BranchCandidate:
    """One ranked chunk from a single branch (rank starts at 1)."""

    chunk: ChunkRef
    branch: Branch
    rank: int
    score: float
    source_published_at: datetime | None
    text: str
    vector_generation: int | None


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """Chunk after RRF (and dedup, which may raise ``fused_score``)."""

    chunk: ChunkRef
    fused_score: float
    own_fused_score: float
    lexical_rank: int | None
    vector_rank: int | None
    source_published_at: datetime | None
    text: str
    vector_generation: int | None
    duplicate_chunk_ids: tuple[UUID, ...] = ()
    duplicate_article_ids: tuple[UUID, ...] = ()

    @property
    def branches(self) -> tuple[Branch, ...]:
        """Branches in which this chunk appeared."""
        found: list[Branch] = []
        if self.lexical_rank is not None:
            found.append(Branch.LEXICAL)
        if self.vector_rank is not None:
            found.append(Branch.VECTOR)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """Final candidate order after rerank (or fused fallback)."""

    candidate: FusedCandidate
    rerank_score: float | None
    position: int


@dataclass(frozen=True, slots=True)
class MetadataProvenance:
    """Identity of the valid READY publication used for metadata filters."""

    enrichment_version: str
    model_version: str
    prompt_version: str
    index_generation: int


@dataclass(frozen=True, slots=True)
class RetrievedSpan:
    """Evidence span plus its chunk identity and ranking provenance."""

    span: EvidenceSpan
    chunk_id: UUID
    chunk_index: int
    branches: tuple[Branch, ...]
    lexical_rank: int | None
    vector_rank: int | None
    fused_score: float
    rerank_score: float | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class RetrievedArticle:
    """Allowlisted source fields and spans for one evidence article."""

    rank: int
    article_id: UUID
    content_hash: str
    title: str
    source_name: str
    source_domain: str
    source_published_at: datetime | None
    retrieval_basis: RetrievalMode
    vector_generation: int | None
    metadata: MetadataProvenance | None
    duplicate_article_ids: tuple[UUID, ...]
    spans: tuple[RetrievedSpan, ...]


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    """Serialized, budgeted evidence handed to synthesis (T6)."""

    context_format: str
    text: str
    token_count: int
    token_budget: int
    tokenizer_id: str
    tokenizer_version: str
    truncated_spans: int
    omitted_articles: int


@dataclass(frozen=True, slots=True)
class FilterCoverage:
    """Metadata-filter coverage; COMPLETE when no metadata filter applies."""

    status: CoverageStatus
    metadata_filter_requested: bool
    scoped_articles: int
    verifiable_articles: int
    unverifiable_articles: int
    matching_articles: int
    scan_cap_reached: bool


@dataclass(frozen=True, slots=True)
class StageMetric:
    """Latency and candidate counts for one stage (no content)."""

    stage: str
    latency_ms: float
    candidates_in: int
    candidates_out: int


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """Query metrics; never contain article text, prompts or storage URIs."""

    stages: tuple[StageMetric, ...]
    storage_calls: int
    embedding_calls: int
    rerank_calls: int
    suppressed_duplicates: int
    dropped_at_final_check: int
    lexical_truncated_articles: int


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    """Versions that make a result reproducible."""

    policy_id: str
    fusion_policy_id: str
    index_version: str
    chunker_version: str
    normalization_version: str
    embedding_model: str
    embedding_version: str
    corpus_generation: str
    reranker_id: str | None
    reranker_version: str | None
    publication_time_basis: Literal["source_published_at"]
    query_time: datetime


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Typed use-case result consumed by synthesis (T6) and the API (T7)."""

    status: ResultStatus
    empty_reason: EmptyReason | None
    retrieval_mode: RetrievalMode
    vector_branch_status: VectorBranchStatus
    rerank_status: RerankStatus
    rerank_degrade_reason: RerankDegradeReason | None
    coverage: FilterCoverage
    articles: tuple[RetrievedArticle, ...]
    context: EvidenceContext
    provenance: RetrievalProvenance
    metrics: RetrievalMetrics


def text_terms(text: str) -> tuple[str, ...]:
    """Casefolded word tokens for dedup similarity only (never search/offsets)."""
    folded = normalize("NFC", text).casefold()
    return tuple(match.group(0) for match in _WORD.finditer(folded))


def dedup_fingerprint(text: str) -> str:
    """Exact-duplicate fingerprint: casefold NFC text, whitespace collapsed."""
    folded = _SPACE.sub(" ", normalize("NFC", text).casefold()).strip()
    return f"sha256:{sha256(folded.encode()).hexdigest()}"


def span_id_for(article_id: UUID, content_hash: str, start: int, end: int) -> UUID:
    """Deterministic span ID; a new range gets a new ID."""
    return uuid5(SPAN_NAMESPACE, f"{article_id}|{content_hash}|{start}|{end}")


def build_span(
    article_id: UUID, content_hash: str, content: str, start: int, end: int
) -> EvidenceSpan:
    """Validated span whose excerpt is the exact code-point substring."""
    excerpt = content[start:end]
    return EvidenceSpan(
        span_id=span_id_for(article_id, content_hash, start, end),
        article_id=article_id,
        content_hash=content_hash,
        start_offset=start,
        end_offset=end,
        excerpt=excerpt,
        span_fingerprint=f"sha256:{sha256(excerpt.encode()).hexdigest()}",
        offset_basis="unicode_codepoint",
    )


@dataclass(frozen=True, slots=True)
class LexicalLimits:
    """Frozen lexical scan bounds passed to storage (policy v2, R3/R4)."""

    stopwords: tuple[str, ...]
    max_terms: int
    article_pool: int
    chunks_scanned_per_article: int
    chunks_per_article: int
    max_metadata_candidates: int
    chunk_size: int
    chunk_overlap: int


@dataclass(frozen=True, slots=True)
class LexicalChunk:
    """One scored chunk window of a current document (metadata only, no text).

    Storage keeps every window tied at the per-article cut (SQL ``rank()``), so
    the exact frozen tie-break, which needs ``chunk_id``, is applied in Python.
    """

    article_id: UUID
    content_hash: str
    source_published_at: datetime | None
    chunk_index: int
    start_offset: int
    end_offset: int
    matched_terms: int
    document_rank: float


@dataclass(frozen=True, slots=True)
class LexicalPage:
    """Bounded lexical result: query lexeme count, chunks, scan truncation."""

    term_count: int
    chunks: tuple[LexicalChunk, ...]
    truncated_articles: int


@dataclass(frozen=True, slots=True)
class VectorHit:
    """Active, identity-compatible chunk with its exact cosine distance."""

    article_id: UUID
    content_hash: str
    generation: int
    chunk_index: int
    start_offset: int
    end_offset: int
    content: str
    source_published_at: datetime | None
    distance: float
