"""Search-index projection, filters and rank fusion (framework-free).

PostgreSQL stays the source of truth. A search index only holds a rebuildable
projection of the current revision of each article: one document per chunk,
tied to ``article_id``, ``revision_id``, ``content_hash``, ``chunk_version``
and the index generation that produced it. Nothing here performs I/O.
"""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import ClassVar, Final
from uuid import UUID, uuid5

from backend.rag.domain.retrieval import CHUNK_NAMESPACE, ChunkRef

REVISION_NAMESPACE: Final = UUID("2d6a1c3e-8b4f-4e7a-9c15-7f3e0b9a6d41")

DEFAULT_BM25_DEPTH: Final = 50

DEFAULT_SEMANTIC_DEPTH: Final = 50

DEFAULT_RRF_K: Final = 60

DEFAULT_CANDIDATE_POOL: Final = 45

MAX_BRANCH_DEPTH: Final = 100


class SearchMode(StrEnum):
    """Which branches a search runs."""

    LEXICAL_ONLY = "lexical_only"
    SEMANTIC_ONLY = "semantic_only"
    HYBRID = "hybrid"
    NONE = "none"


class SearchBranch(StrEnum):
    """Candidate-producing branch of a hybrid search."""

    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class GenerationStatus(StrEnum):
    """Lifecycle of one search index generation."""

    BUILDING = "BUILDING"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class InvalidSearchConfigError(ValueError):
    """Search depths and fusion parameters must be positive and bounded."""

    def __init__(self, detail: str) -> None:
        """Name the violated bound."""
        super().__init__(f"invalid search configuration: {detail}")


def revision_id_for(article_id: UUID, content_hash: str) -> UUID:
    """Stable revision ID of ``(article_id, content_hash)`` (PG revision key)."""
    return uuid5(REVISION_NAMESPACE, f"{article_id}|{content_hash}")


def search_chunk_id(
    article_id: UUID, content_hash: str, chunk_version: str, index: int
) -> UUID:
    """Chunk ID; the chunk version is part of the identity.

    Same formula as ``ChunkRef.chunk_id``, so a chunk keeps one ID whether it
    was found by PostgreSQL retrieval or by the search index.
    """
    return uuid5(
        CHUNK_NAMESPACE, f"{article_id}|{content_hash}|{chunk_version}|{index}"
    )


VISIBILITY_SCOPES: Final[dict[str, tuple[str, ...]]] = {
    "internal": ("fixture-internal",),
}


def scopes_for_visibility(visibility: str) -> tuple[str, ...]:
    """Caller scopes a projected document is visible to (fail closed)."""
    return VISIBILITY_SCOPES.get(visibility, ())


def text_hash(text: str) -> str:
    """Exact SHA-256 of chunk text; the embedding cache key with the model."""
    return f"sha256:{sha256(text.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class IndexGeneration:
    """One rebuildable index build: chunker + embedding identity + index name."""

    generation_id: int
    index_name: str
    chunk_version: str
    embedding_identity: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    status: GenerationStatus

    @property
    def label(self) -> str:
        """Human/diagnostic label, stable across processes."""
        model = self.embedding_identity or "lexical"
        return f"g{self.generation_id}:{self.chunk_version}:{model}"


@dataclass(frozen=True, slots=True)
class SearchChunkDocument:
    """One projected chunk; every field is derivable from PostgreSQL."""

    article_id: UUID
    revision_id: UUID
    chunk_id: UUID
    content_hash: str
    chunk_version: str
    chunk_index: int
    char_start: int
    char_end: int
    paragraph_start: int
    paragraph_end: int
    title: str
    sapo: str
    text: str
    source_name: str
    source_domain: str
    published_at: datetime | None
    topics: tuple[str, ...]
    entity_ids: tuple[str, ...]
    embedding: tuple[float, ...] | None
    embedding_model: str | None
    embedding_identity: str | None
    embedding_dimensions: int | None
    generation_id: int
    access_scopes: tuple[str, ...]
    is_current: bool = True
    tombstoned: bool = False
    projection_version: int = 0


@dataclass(frozen=True, slots=True)
class RevisionDigest:
    """What reconcile compares per article: current revision and chunk count."""

    article_id: UUID
    content_hash: str
    chunks: int


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Filters every branch applies identically (non-scoring).

    ``published_after`` inclusive, ``published_before`` exclusive; a missing
    publication time never satisfies a bound. Topics any-of, entities any-of,
    AND across kinds. ``allowed_article_ids`` is ``None`` when unrestricted.
    ``scope_id`` restricts to documents visible to the caller's scope.
    """

    scope_id: str
    source_domains: tuple[str, ...] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None
    topics: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    allowed_article_ids: tuple[UUID, ...] | None = None

    @property
    def empty_range(self) -> bool:
        """Equal bounds select nothing (same rule as PostgreSQL retrieval)."""
        return (
            self.published_after is not None
            and self.published_after == self.published_before
        )


@dataclass(frozen=True, slots=True)
class HybridSearchConfig:
    """Hybrid defaults: BM25 top 50 + semantic top 50, RRF k=60, pool 45."""

    bm25_depth: int = DEFAULT_BM25_DEPTH
    semantic_depth: int = DEFAULT_SEMANTIC_DEPTH
    rrf_k: int = DEFAULT_RRF_K
    candidate_pool: int = DEFAULT_CANDIDATE_POOL

    def __post_init__(self) -> None:
        """Bound depths so a request can never scan an unbounded window."""
        depths = {
            "bm25_depth": self.bm25_depth,
            "semantic_depth": self.semantic_depth,
            "candidate_pool": self.candidate_pool,
        }
        for name, value in depths.items():
            if not 0 < value <= MAX_BRANCH_DEPTH:
                msg = f"0 < {name} <= {MAX_BRANCH_DEPTH}"
                raise InvalidSearchConfigError(msg)
        if self.rrf_k <= 0:
            msg = "rrf_k > 0"
            raise InvalidSearchConfigError(msg)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked chunk from one branch (rank starts at 1)."""

    chunk_id: UUID
    article_id: UUID
    revision_id: UUID
    content_hash: str
    chunk_version: str
    chunk_index: int
    char_start: int
    char_end: int
    text: str
    published_at: datetime | None
    source_domain: str
    generation_id: int
    branch: SearchBranch
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class FusedHit:
    """Chunk after reciprocal rank fusion, with per-branch provenance."""

    hit: SearchHit
    lexical_rank: int | None
    semantic_rank: int | None
    fused_rank: int
    fused_score: float
    lexical_contribution: float
    semantic_contribution: float

    @property
    def branches(self) -> tuple[SearchBranch, ...]:
        """Branches that returned this chunk."""
        found: list[SearchBranch] = []
        if self.lexical_rank is not None:
            found.append(SearchBranch.LEXICAL)
        if self.semantic_rank is not None:
            found.append(SearchBranch.SEMANTIC)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Fused candidates plus what made them reproducible."""

    mode: SearchMode
    generation: IndexGeneration | None
    hits: tuple[FusedHit, ...]
    lexical_count: int
    semantic_count: int
    latency_ms: dict[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True, slots=True)
class RevisionWrite:
    """Projection of one article state at ``version`` (no documents = delete).

    Writes are fenced per article: an index applies a write only when no
    write with a higher ``version`` was applied before, and removes its own
    chunks again when a newer write overtakes it while in flight.
    """

    article_id: UUID
    version: int
    documents: tuple[SearchChunkDocument, ...]


def rrf_fuse(
    lexical: tuple[SearchHit, ...],
    semantic: tuple[SearchHit, ...],
    k: int = DEFAULT_RRF_K,
    pool: int = DEFAULT_CANDIDATE_POOL,
) -> tuple[FusedHit, ...]:
    """Reciprocal rank fusion; raw scores are never added across branches.

    Each branch contributes ``1 / (k + rank)``. Ties break on the best single
    rank, then on newer publication time, then on ``chunk_id``, so the order is
    deterministic. At most ``pool`` candidates are returned.
    """
    by_chunk: dict[UUID, tuple[SearchHit, int | None, int | None]] = {}
    for hit in lexical:
        _, _, sem = by_chunk.get(hit.chunk_id, (hit, None, None))
        by_chunk[hit.chunk_id] = (hit, hit.rank, sem)
    for hit in semantic:
        base, lex, _ = by_chunk.get(hit.chunk_id, (hit, None, None))
        by_chunk[hit.chunk_id] = (base, lex, hit.rank)
    scored: list[tuple[float, int, float, str, SearchHit, int | None, int | None]] = []
    for chunk_id, (hit, lex, sem) in by_chunk.items():
        lex_part = 0.0 if lex is None else 1.0 / (k + lex)
        sem_part = 0.0 if sem is None else 1.0 / (k + sem)
        best = min(rank for rank in (lex, sem) if rank is not None)
        published = 0.0 if hit.published_at is None else hit.published_at.timestamp()
        scored.append(
            (lex_part + sem_part, best, published, str(chunk_id), hit, lex, sem)
        )
    scored.sort(key=lambda item: (-item[0], item[1], -item[2], item[3]))
    return tuple(
        FusedHit(
            hit=hit,
            lexical_rank=lex,
            semantic_rank=sem,
            fused_rank=position,
            fused_score=score,
            lexical_contribution=0.0 if lex is None else 1.0 / (k + lex),
            semantic_contribution=0.0 if sem is None else 1.0 / (k + sem),
        )
        for position, (score, _, _, _, hit, lex, sem) in enumerate(
            scored[:pool], start=1
        )
    )


class MissingProjectionVersionError(ValueError):
    """A fenced write needs a version (explicit or carried by its chunks)."""

    def __init__(self) -> None:
        """Constant message."""
        super().__init__("fenced write without a projection version")


def fence_version(
    documents: tuple[SearchChunkDocument, ...], version: int | None
) -> int:
    """Explicit version, else the single ``projection_version`` of the chunks."""
    if version is not None:
        return version
    versions = {document.projection_version for document in documents}
    if len(versions) != 1:
        raise MissingProjectionVersionError
    return versions.pop()


def stamped(writes: tuple["RevisionWrite", ...]) -> tuple["RevisionWrite", ...]:
    """Every chunk of a write carries the write's version (the fence key)."""
    return tuple(
        replace(
            write,
            documents=tuple(
                replace(document, projection_version=write.version)
                for document in write.documents
            ),
        )
        for write in writes
    )


def search_mode(lexical: int, semantic: int) -> SearchMode:
    """Mode label from the branches that actually returned candidates."""
    if lexical and semantic:
        return SearchMode.HYBRID
    if lexical:
        return SearchMode.LEXICAL_ONLY
    if semantic:
        return SearchMode.SEMANTIC_ONLY
    return SearchMode.NONE


CHUNK_TEXT_TEMPLATE = "{title}\n\n{content}"


class InvalidEmbeddingError(ValueError):
    """An embedder returned a vector of the wrong size or non-finite values."""

    code: ClassVar[str] = "embedding_invalid"

    def __init__(self, expected: int, actual: int) -> None:
        """Describe the shape problem without any vector values."""
        super().__init__(f"expected {expected} finite values, got {actual}")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk window plus the filter fields every engine indexes."""

    chunk: ChunkRef
    title: str
    content: str
    source_domain: str
    source_published_at: datetime | None

    @property
    def text(self) -> str:
        """Frozen engine input (``CHUNK_TEXT_TEMPLATE``)."""
        return CHUNK_TEXT_TEMPLATE.format(title=self.title, content=self.content)


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """Engine hit; ``score`` is engine-specific and only ordered within one run."""

    chunk: ChunkRef
    score: float
    source_published_at: datetime | None


@dataclass(frozen=True, slots=True)
class LexicalIndexIdentity:
    """Everything that changes lexical ranking; recorded in every report."""

    engine: str
    engine_version: str
    analyzer_id: str
    analyzer_digest: str
    similarity: str


@dataclass(frozen=True, slots=True)
class EmbedderIdentity:
    """Model, revision and input conventions of a real or fake embedder.

    Unlike the production ``EmbeddingIdentity`` (fixed 1536 schema), this
    identity accepts any dimension: evaluation vectors live in their own
    per-identity tables and never in ``rag_chunks``.
    """

    model: str
    revision: str
    dimension: int
    pooling: str
    query_prefix: str
    document_prefix: str
    max_tokens: int
    normalized: bool = True

    @property
    def slug(self) -> str:
        """Stable short name for tables and reports."""
        base = "".join(ch if ch.isalnum() else "_" for ch in self.model.lower())
        return f"{base}_{self.dimension}"[:48].strip("_")


def checked_vector(values: tuple[float, ...], dimension: int) -> tuple[float, ...]:
    """Reject wrong sizes and non-finite values; never pad or truncate."""
    if len(values) != dimension or not all(math.isfinite(v) for v in values):
        raise InvalidEmbeddingError(dimension, len(values))
    return values
