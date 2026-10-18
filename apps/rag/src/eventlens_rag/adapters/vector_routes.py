"""Exact-vs-ANN vector route measurement on an isolated, empty RAG database.

The production route stays exact (policy ``vector.route = "exact"``). This
module loads a deterministic synthetic corpus through the real schema, then
compares the production exact query with an HNSW iterative-scan query on the
same corpus, embeddings, queries and filters. It never runs against a database
that already holds articles.
"""

# SQL is assembled only from module-level literal constants; values are bound.
# ruff: noqa: S608

import math
import random
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow
from pydantic import TypeAdapter

from eventlens_rag.adapters.connection import connect
from eventlens_rag.adapters.postgres import persist_revision
from eventlens_rag.adapters.postgres_retrieval import (
    SCOPE_FILTER,
    VECTOR_EXACT_SQL,
    PostgresRetrievalStore,
    identity_params,
    scope_params,
    vector_literal,
)
from eventlens_rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.ports.retrieval import RetrievalScope

DIMENSION = 1536
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HNSW_INDEX = "rag_chunks_embedding_hnsw_idx"

ANN_SETTINGS: LiteralString = """SELECT
    set_config('hnsw.ef_search', %(ef)s, true),
    set_config('hnsw.iterative_scan', 'relaxed_order', true),
    set_config('hnsw.max_scan_tuples', %(scan)s, true),
    set_config('enable_seqscan', %(force)s, true),
    set_config('enable_sort', %(force)s, true)"""

ANN_SQL: LiteralString = (
    """
WITH ann AS MATERIALIZED (
    SELECT c.article_id, c.chunk_index,
           (c.embedding <=> %(embedding)s::vector(1536))::float8 AS distance
    FROM rag_chunks c
    WHERE EXISTS (
        SELECT 1 FROM rag_article_current cur
        JOIN rag_index_generations g ON g.generation = cur.active_index_generation
        JOIN rag_article_documents d
            ON d.article_id = cur.article_id AND d.content_hash = cur.content_hash
        WHERE cur.article_id = c.article_id AND cur.content_hash = c.content_hash
          AND cur.active_index_generation = c.generation
          AND cur.tombstoned_at IS NULL AND g.status = 'ACTIVE'
          AND g.embedding_model = %(model)s AND g.embedding_version = %(version)s
          AND g.embedding_dimension = %(dimension)s
          AND g.chunker_version = %(chunker)s
          AND g.normalization_version = %(normalization)s
          AND g.corpus_generation = %(corpus)s
          AND """
    + SCOPE_FILTER
    + """)
    ORDER BY c.embedding <=> %(embedding)s::vector(1536)
    LIMIT %(limit)s
)
SELECT article_id::text, chunk_index, distance FROM ann
ORDER BY distance ASC, article_id ASC, chunk_index ASC
"""
)


class CorpusNotEmptyError(RuntimeError):
    """The measurement refuses to touch a database that already has articles."""

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("vector route report needs an empty, isolated RAG database")


@dataclass(frozen=True, slots=True)
class Scenario:
    """One filter selectivity level."""

    name: str
    scope: RetrievalScope
    eligible: int


@dataclass(frozen=True, slots=True)
class RouteResult:
    """Per-scenario, per-route measurement (no article content)."""

    scenario: str
    route: str
    eligible_chunks: int
    k: int
    queries: int
    recall_at_k: float
    recall_denominator: int
    latency_ms_median: float
    latency_ms_p95: float
    statements_per_query: int
    uses_hnsw_index: bool
    plan_file: str


def unit_vector(rng: random.Random) -> tuple[float, ...]:
    """Deterministic L2-normalized Gaussian vector."""
    values = [rng.gauss(0.0, 1.0) for _ in range(DIMENSION)]
    norm = math.sqrt(math.fsum(v * v for v in values))
    return tuple(v / norm for v in values)


def _document(index: int) -> ArticleDocument:
    content = f"Synthetic vector route article {index}."
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": UUID(int=index + 1),
            "source_id": UUID(int=10**6),
            "title": f"Synthetic {index}",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": f"https://synthetic.example/{index}",
            "source_name": "Synthetic",
            "source_domain": f"d{index % 100}.example",
            "source_published_at": T0 + timedelta(minutes=index),
            "ingested_at": T0,
            "processed_at": T0,
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


def load_corpus(database_url: str, articles: int, seed: int) -> None:
    """Persist ``articles`` one-chunk documents with active synthetic vectors."""
    identity = EmbeddingIdentity()
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic data
    with connect(database_url) as connection:
        count = connection.execute("SELECT count(*) FROM rag_article_documents")
        row = count.fetchone()
        if row is None or row[0] != 0:
            raise CorpusNotEmptyError
        for index in range(articles):
            document = _document(index)
            persist_revision(connection, document)
            generation = connection.execute(
                """INSERT INTO rag_index_generations (
                    article_id, content_hash, embedding_model, embedding_version,
                    embedding_dimension, chunker_version, normalization_version,
                    corpus_generation, expected_chunks, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,'ACTIVE') RETURNING generation""",
                (
                    document.article_id,
                    document.content_hash,
                    identity.model,
                    identity.version,
                    identity.dimension,
                    identity.chunker_version,
                    identity.normalization_version,
                    identity.corpus_generation,
                ),
            ).fetchone()
            assert generation is not None  # noqa: S101 - RETURNING row
            _ = connection.execute(
                """INSERT INTO rag_chunks (
                    chunk_id, article_id, content_hash, generation, chunk_index,
                    start_offset, end_offset, content, fingerprint, embedding
                ) VALUES (%s,%s,%s,%s,0,0,%s,%s,%s,%s::vector(1536))""",
                (
                    UUID(int=10**7 + index),
                    document.article_id,
                    document.content_hash,
                    generation[0],
                    len(document.normalized_content),
                    document.normalized_content,
                    document.content_hash,
                    vector_literal(unit_vector(rng)),
                ),
            )
            _ = connection.execute(
                """UPDATE rag_article_current SET active_index_generation = %s
                WHERE article_id = %s""",
                (generation[0], document.article_id),
            )
        _ = connection.execute("ANALYZE")


def _plan(
    connection: psycopg.Connection[TupleRow],
    query: LiteralString,
    params: dict[str, object],
) -> str:
    rows = connection.execute("EXPLAIN (ANALYZE, COSTS) " + query, params).fetchall()
    return "\n".join(TypeAdapter(str).validate_python(row[0]) for row in rows)


def _ann(
    database_url: str,
    params: dict[str, object],
    *,
    force_index: bool,
    explain: bool,
) -> tuple[list[tuple[str, int]], str]:
    # Forcing (seqscan/sort off) exists only here, to exercise the HNSW
    # mechanism on small corpora; it is never used by the production path.
    settings = {"ef": "40", "scan": "20000", "force": "off" if force_index else "on"}
    with connect(database_url) as connection:
        connection.read_only = True
        _ = connection.execute(ANN_SETTINGS, settings)
        if explain:
            return [], _plan(connection, ANN_SQL, params)
        rows = TypeAdapter(list[tuple[str, int, float]]).validate_python(
            connection.execute(ANN_SQL, params).fetchall()
        )
    return [(article, chunk) for article, chunk, _ in rows], ""


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _scenarios(articles: int) -> list[Scenario]:
    def scope(
        domains: tuple[str, ...] = (),
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> RetrievalScope:
        return RetrievalScope(domains, after, before, None)

    ten = tuple(f"d{i}.example" for i in range(10))
    window = (T0, T0 + timedelta(minutes=articles // 20))
    return [
        Scenario("all_100pct", scope(), articles),
        Scenario("source_10pct", scope(ten), articles // 10),
        Scenario("source_1pct", scope(("d0.example",)), articles // 100),
        Scenario("time_5pct", scope(after=window[0], before=window[1]), articles // 20),
    ]


def measure(
    database_url: str, output: Path, *, queries: int, k: int, seed: int
) -> list[RouteResult]:
    """Run exact vs ANN (natural plan and forced index) for every scenario."""
    identity = EmbeddingIdentity()
    rng = random.Random(seed + 1)  # noqa: S311 - deterministic query vectors
    vectors = [unit_vector(rng) for _ in range(queries)]
    with connect(database_url) as connection:
        count = connection.execute("SELECT count(*) FROM rag_current_vectors")
        row = count.fetchone()
    articles = 0 if row is None else TypeAdapter(int).validate_python(row[0])
    results: list[RouteResult] = []
    for scenario in _scenarios(articles):
        exact: list[set[tuple[str, int]]] = []
        exact_ms: list[float] = []
        for vector in vectors:
            store = PostgresRetrievalStore(database_url)
            started = time.perf_counter()
            hits = store.vector_chunks(vector, identity, scenario.scope, k)
            exact_ms.append((time.perf_counter() - started) * 1000)
            exact.append({(str(h.article_id), h.chunk_index) for h in hits})
        params: dict[str, object] = {
            **scope_params(scenario.scope),
            **identity_params(identity),
            "embedding": vector_literal(vectors[0]),
            "limit": k,
        }
        with connect(database_url) as connection:
            exact_plan = _plan(connection, VECTOR_EXACT_SQL, params)
        plan_name = f"explain-{scenario.name}-exact.txt"
        _ = (output / plan_name).write_text(exact_plan, encoding="utf-8")
        results.append(
            RouteResult(
                scenario.name,
                "exact",
                scenario.eligible,
                k,
                queries,
                1.0,
                sum(len(e) for e in exact),
                statistics.median(exact_ms),
                _percentile(exact_ms, 0.95),
                1,
                HNSW_INDEX in exact_plan,
                plan_name,
            )
        )
        for route, force in (("ann_natural_plan", False), ("ann_forced_index", True)):
            found = 0
            latencies: list[float] = []
            for vector, truth in zip(vectors, exact, strict=True):
                started = time.perf_counter()
                rows, _ = _ann(
                    database_url,
                    {**params, "embedding": vector_literal(vector)},
                    force_index=force,
                    explain=False,
                )
                latencies.append((time.perf_counter() - started) * 1000)
                found += len(truth & set(rows))
            _, plan = _ann(database_url, params, force_index=force, explain=True)
            plan_name = f"explain-{scenario.name}-{route}.txt"
            _ = (output / plan_name).write_text(plan, encoding="utf-8")
            denominator = sum(len(e) for e in exact)
            results.append(
                RouteResult(
                    scenario.name,
                    route,
                    scenario.eligible,
                    k,
                    queries,
                    found / denominator if denominator else 1.0,
                    denominator,
                    statistics.median(latencies),
                    _percentile(latencies, 0.95),
                    2,
                    HNSW_INDEX in plan,
                    plan_name,
                )
            )
    return results


def pgvector_version(database_url: str) -> str:
    """Installed pgvector extension version."""
    with connect(database_url) as connection:
        row = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    return "" if row is None else TypeAdapter(str).validate_python(row[0])


def report_rows(results: list[RouteResult]) -> list[dict[str, object]]:
    """JSON-ready rows."""
    return [asdict(result) for result in results]
