"""Reranker boundary: reorder a filtered candidate set, never extend it."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """Token-truncated chunk text; truncation keeps an exact source prefix."""

    candidate_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class RerankRequest:
    """Query plus at most 40 candidates and the call's own time slice."""

    query: str
    candidates: tuple[RerankCandidate, ...]
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class RerankOutput:
    """One score per candidate ID; validated at runtime by the caller.

    Scores are typed loosely on purpose: adapters are untrusted and the
    application rejects unknown, duplicate or missing IDs and non-finite scores.
    """

    scores: tuple[tuple[object, object], ...]


@dataclass(frozen=True, slots=True)
class RerankerIdentity:
    """Reranker ID/version recorded in every retrieval result."""

    reranker_id: str
    version: str


class RerankerPort(Protocol):
    """Score candidates; must not add, edit or filter evidence."""

    @property
    def identity(self) -> RerankerIdentity:
        """Stable reranker identity."""
        ...

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """Return scores for exactly the requested candidate IDs."""
        ...
