"""Alternative candidate source for retrieval (e.g. a search index).

``RetrieveEvidence`` asks a ``CandidateSource`` for lexical and semantic
branch candidates instead of reading PostgreSQL branches. Everything after
the branches (RRF, dedup, rerank, the PostgreSQL final current/tombstone/
scope check and evidence assembly) is unchanged, so an index can never widen
what a caller sees. ``CandidateSourceUnavailableError`` lets retrieval fall
back to PostgreSQL when the policy allows it.
"""

from dataclasses import dataclass
from typing import ClassVar, Protocol

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.access import CallerScope
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    BranchCandidate,
    QueryControl,
    RetrievalError,
    VectorBranchStatus,
)
from backend.rag.domain.retrieval_tuning import RetrievalTuning


class CandidateSourceUnavailableError(RetrievalError):
    """The source cannot answer now; ``reason`` is a stable code."""

    code: ClassVar[str] = "search_index_unavailable"
    reason: str

    def __init__(self, reason: str) -> None:
        """Keep a stable reason code, never engine payloads."""
        super().__init__(f"candidate source unavailable: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    """Branch candidates plus the identity of the index that produced them."""

    lexical: tuple[BranchCandidate, ...]
    vector: tuple[BranchCandidate, ...]
    vector_status: VectorBranchStatus
    term_count: int | None
    backend: str
    generation: str
    chunker_version: str
    embedding_identity: str | None
    storage_calls: int
    embedding_calls: int
    branch_cap_dropped: int = 0
    backfill_calls: int = 0


class CandidateSource(Protocol):
    """Filtered lexical/semantic candidates for one query text."""

    def candidates(  # noqa: PLR0913 - explicit per-call switches
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
        """Both branches with identical filters, or raise a typed error.

        ``tuning`` (``None`` = baseline) carries the per-article branch cap,
        its bounded backfill and BM25 field boosts.
        """
        ...
