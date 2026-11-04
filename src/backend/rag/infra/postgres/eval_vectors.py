"""Evaluation-only pgvector store: one table per embedder identity.

Production vectors stay in ``rag_chunks`` with the reviewed ``vector(1536)``
column. Benchmark vectors of other models/dimensions live in the separate
``rag_eval`` schema, in ``rag_eval.vectors_<slug>`` tables typed with the
model's own dimension, so nothing is padded or truncated to fit. This schema
is created by the benchmark tool, not by Alembic: it is disposable and must be
built only in an isolated evaluation database (``require_eval_database``).

Search is exact (sequential cosine in a MATERIALIZED CTE, no ANN index) and
joins the current, untombstoned revision so stale or tombstoned chunks never
match; scope filters are the production ``SCOPE_FILTER``.
"""

# SQL is assembled only from validated identifiers and module constants.
# ruff: noqa: S608

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Engine

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.retrieval import ChunkRef
from backend.rag.domain.search_index import (
    EmbedderIdentity,
    IndexedChunk,
    ScoredChunk,
    checked_vector,
)
from backend.rag.infra.postgres.engine import execute, scalar, transaction
from backend.rag.infra.retrieval.postgres_retrieval import (
    SCOPE_FILTER,
    scope_params,
    vector_literal,
)

EVAL_SCHEMA: Final = "rag_eval"
MAX_DIMENSION: Final = 4096
INSERT_BATCH: Final = 200
_SLUG = re.compile(r"^[a-z0-9_]{1,48}$")
_SCHEMA = re.compile(r"^rag_eval[a-z0-9_]{0,40}$")
# Database names that hold real data and never receive the eval schema.
PROTECTED_DATABASES: Final = frozenset({"eventlens", "eventlens_rag", "postgres"})

_HITS: TypeAdapter[
    list[tuple[str, str, str, int, int, int, datetime | None, float]]
] = TypeAdapter(list[tuple[str, str, str, int, int, int, datetime | None, float]])


class EvalDatabaseRefusedError(RuntimeError):
    """The target database is not an isolated evaluation database."""

    code: ClassVar[str] = "eval_database_refused"


def require_eval_database(engine: Engine) -> str:
    """Refuse protected database names; return the current database name."""
    with transaction(engine, read_only=True) as connection:
        name = str(scalar(execute(connection, "SELECT current_database()")))
    if name in PROTECTED_DATABASES or not name.endswith("_eval"):
        msg = f"database {name!r} is not an evaluation database (*_eval)"
        raise EvalDatabaseRefusedError(msg)
    return name


def table_for(identity: EmbedderIdentity, schema: str = EVAL_SCHEMA) -> str:
    """Qualified table name for one identity (validated identifiers)."""
    slug = identity.slug
    if not _SLUG.fullmatch(slug) or not _SCHEMA.fullmatch(schema):
        msg = f"invalid evaluation identifier {schema!r}.{slug!r}"
        raise ValueError(msg)
    return f"{schema}.vectors_{slug}"


def _dimension(identity: EmbedderIdentity) -> int:
    if not 0 < identity.dimension <= MAX_DIMENSION:
        msg = f"unsupported evaluation dimension {identity.dimension}"
        raise ValueError(msg)
    return int(identity.dimension)


@dataclass(frozen=True, slots=True)
class PostgresEvalVectors:
    """``ChunkVectorIndex`` over ``rag_eval.vectors_<slug>`` tables."""

    engine: Engine
    schema: str = EVAL_SCHEMA

    def rebuild(
        self,
        identity: EmbedderIdentity,
        chunks: Sequence[IndexedChunk],
        vectors: Sequence[tuple[float, ...]],
    ) -> None:
        """Recreate the identity's table and insert every vector, atomically."""
        if len(chunks) != len(vectors):
            msg = "one vector per chunk required"
            raise ValueError(msg)
        table = table_for(identity, self.schema)
        dimension = _dimension(identity)
        with transaction(self.engine) as connection:
            _ = execute(connection, f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            _ = execute(
                connection,
                f"""CREATE TABLE IF NOT EXISTS {self.schema}.vector_sets (
                    slug text PRIMARY KEY, model text NOT NULL,
                    revision text NOT NULL, dimension integer NOT NULL,
                    pooling text NOT NULL, query_prefix text NOT NULL,
                    chunk_count integer NOT NULL,
                    built_at timestamptz NOT NULL DEFAULT now())""",
            )
            _ = execute(connection, f"DROP TABLE IF EXISTS {table}")
            _ = execute(
                connection,
                f"""CREATE TABLE {table} (
                    chunk_id uuid PRIMARY KEY,
                    article_id uuid NOT NULL,
                    content_hash text NOT NULL,
                    chunker_version text NOT NULL,
                    chunk_index integer NOT NULL,
                    start_offset integer NOT NULL,
                    end_offset integer NOT NULL,
                    embedding vector({dimension}) NOT NULL)""",
            )
            rows = [
                (
                    item.chunk.chunk_id,
                    item.chunk.article_id,
                    item.chunk.content_hash,
                    item.chunk.chunker_version,
                    item.chunk.chunk_index,
                    item.chunk.start_offset,
                    item.chunk.end_offset,
                    vector_literal(checked_vector(vector, dimension)),
                )
                for item, vector in zip(chunks, vectors, strict=True)
            ]
            for start in range(0, len(rows), INSERT_BATCH):
                batch = rows[start : start + INSERT_BATCH]
                placeholders = ",".join(
                    f"(%s,%s,%s,%s,%s,%s,%s,%s::vector({dimension}))" for _ in batch
                )
                _ = execute(
                    connection,
                    f"""INSERT INTO {table} (chunk_id, article_id, content_hash,
                        chunker_version, chunk_index, start_offset, end_offset,
                        embedding) VALUES {placeholders}""",
                    tuple(value for row in batch for value in row),
                )
            _ = execute(
                connection,
                f"""INSERT INTO {self.schema}.vector_sets (slug, model, revision,
                    dimension, pooling, query_prefix, chunk_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slug) DO UPDATE SET model = EXCLUDED.model,
                    revision = EXCLUDED.revision, dimension = EXCLUDED.dimension,
                    pooling = EXCLUDED.pooling,
                    query_prefix = EXCLUDED.query_prefix,
                    chunk_count = EXCLUDED.chunk_count, built_at = now()""",
                (
                    identity.slug,
                    identity.model,
                    identity.revision,
                    dimension,
                    identity.pooling,
                    identity.query_prefix,
                    len(rows),
                ),
            )

    def search(
        self,
        identity: EmbedderIdentity,
        vector: tuple[float, ...],
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Exact cosine over current, untombstoned revisions within scope."""
        table = table_for(identity, self.schema)
        dimension = _dimension(identity)
        query = f"""
WITH scored AS MATERIALIZED (
    SELECT v.article_id, v.content_hash, v.chunker_version, v.chunk_index,
           v.start_offset, v.end_offset, d.source_published_at,
           (v.embedding <=> %(embedding)s::vector({dimension}))::float8 AS distance
    FROM {table} v
    JOIN rag_article_current cur
        ON cur.article_id = v.article_id AND cur.content_hash = v.content_hash
       AND cur.tombstoned_at IS NULL
    JOIN rag_article_documents d
        ON d.article_id = v.article_id AND d.content_hash = v.content_hash
    WHERE {SCOPE_FILTER}
)
SELECT article_id::text, content_hash, chunker_version, chunk_index,
       start_offset, end_offset, source_published_at, distance
FROM scored
ORDER BY distance ASC, article_id ASC, chunk_index ASC
LIMIT %(limit)s"""
        with transaction(self.engine, read_only=True) as connection:
            rows = _HITS.validate_python(
                [
                    tuple(row)
                    for row in execute(
                        connection,
                        query,
                        {
                            **scope_params(scope),
                            "embedding": vector_literal(
                                checked_vector(vector, dimension)
                            ),
                            "limit": limit,
                        },
                    )
                ]
            )
        return tuple(
            ScoredChunk(
                chunk=ChunkRef(
                    article_id=UUID(article_id),
                    content_hash=content_hash,
                    chunker_version=chunker,
                    chunk_index=index,
                    start_offset=start,
                    end_offset=end,
                ),
                score=1.0 - distance,
                source_published_at=published,
            )
            for (
                article_id,
                content_hash,
                chunker,
                index,
                start,
                end,
                published,
                distance,
            ) in rows
        )

    def size_bytes(self, identity: EmbedderIdentity) -> int:
        """``pg_total_relation_size`` of the identity's table."""
        with transaction(self.engine, read_only=True) as connection:
            value = scalar(
                execute(
                    connection,
                    "SELECT pg_total_relation_size(%s::regclass)",
                    (table_for(identity, self.schema),),
                )
            )
        return int(str(value))
