"""Exact vs HNSW routes on real pgvector (small synthetic corpus, mechanism only)."""

import math
import random
from pathlib import Path
from uuid import UUID

import pytest

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.infra.diagnostics.vector_routes import (
    CorpusNotEmptyError,
    load_corpus,
    measure,
    pgvector_version,
    unit_vector,
)
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore

ARTICLES = 300
SEED = 11


def cosine_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    norms = math.sqrt(math.fsum(a * a for a in left)) * math.sqrt(
        math.fsum(b * b for b in right)
    )
    return 1.0 - dot / norms


def test_exact_route_matches_brute_force_cosine_under_filters(
    retrieval_url: str,
) -> None:
    load_corpus(retrieval_url, ARTICLES, SEED)
    rng = random.Random(SEED)  # noqa: S311 - same deterministic corpus vectors
    corpus = [unit_vector(rng) for _ in range(ARTICLES)]
    query = unit_vector(random.Random(99))  # noqa: S311
    domains = tuple(f"d{i}.example" for i in range(10))
    scope = RetrievalScope(domains, None, None, None)

    hits = PostgresRetrievalStore(rag_engine(retrieval_url)).vector_chunks(
        query, EmbeddingIdentity(), scope, 10
    )

    eligible = [i for i in range(ARTICLES) if i % 100 < 10]
    expected = sorted(eligible, key=lambda i: cosine_distance(query, corpus[i]))[:10]
    assert [hit.article_id for hit in hits] == [UUID(int=i + 1) for i in expected]
    assert all(hit.distance >= 0 for hit in hits)


def test_report_measures_both_routes_and_refuses_non_empty_db(
    retrieval_url: str, tmp_path: Path
) -> None:
    load_corpus(retrieval_url, ARTICLES, SEED)

    results = measure(retrieval_url, tmp_path, queries=3, k=5, seed=SEED)

    assert pgvector_version(retrieval_url) == "0.8.0"
    routes = {(r.scenario, r.route) for r in results}
    assert ("source_1pct", "ann_forced_index") in routes
    exact = [r for r in results if r.route == "exact"]
    assert all(r.recall_at_k == 1.0 and not r.uses_hnsw_index for r in exact)
    forced = [r for r in results if r.route == "ann_forced_index"]
    assert all(r.uses_hnsw_index for r in forced)
    assert all(0.0 <= r.recall_at_k <= 1.0 for r in results)
    assert all((tmp_path / r.plan_file).read_text(encoding="utf-8") for r in results)
    with pytest.raises(CorpusNotEmptyError):
        load_corpus(retrieval_url, 1, SEED)
