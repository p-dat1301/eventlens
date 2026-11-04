"""Hybrid = two filtered branch requests fused by RRF in Python.

Elasticsearch's own ``rrf`` retriever needs a commercial licence, so fusion
uses the domain's ``rrf_fuse`` (rank-based, never raw-score addition).
"""

from backend.rag.domain.search_index import (
    FusedHit,
    HybridSearchConfig,
    SearchHit,
    rrf_fuse,
)


def fuse(
    lexical: tuple[SearchHit, ...],
    semantic: tuple[SearchHit, ...],
    config: HybridSearchConfig,
) -> tuple[FusedHit, ...]:
    """RRF k and candidate pool from the config."""
    return rrf_fuse(lexical, semantic, config.rrf_k, config.candidate_pool)
