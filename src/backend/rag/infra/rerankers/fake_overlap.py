"""Deterministic term-overlap reranker (mechanics benchmark and tests only).

Scores each candidate by the share of query terms (casefolded, accent-folded)
it contains. It proves rerank plumbing (window, ordering, fallback); it says
nothing about cross-encoder quality and is never selectable by the API.
"""

import re
from dataclasses import dataclass

from backend.rag.application.ports.reranker import (
    RerankerIdentity,
    RerankOutput,
    RerankRequest,
)
from backend.rag.infra.embeddings.fake_bge_m3 import fold

_WORD = re.compile(r"\w+")


def _terms(text: str) -> set[str]:
    return {fold(match.group(0)) for match in _WORD.finditer(text)}


@dataclass(frozen=True, slots=True)
class FakeOverlapReranker:
    """``RerankerPort`` without a model."""

    @property
    def identity(self) -> RerankerIdentity:
        """Stable fake identity."""
        return RerankerIdentity("fake-overlap-reranker", "v1")

    def rerank(self, request: RerankRequest) -> RerankOutput:
        """Query-term coverage per candidate (ties keep input order)."""
        query = _terms(request.query) or {""}
        total = len(request.candidates)
        return RerankOutput(
            scores=tuple(
                (
                    candidate.candidate_id,
                    len(query & _terms(candidate.text)) / len(query)
                    + (total - index) * 1e-6,
                )
                for index, candidate in enumerate(request.candidates)
            )
        )
