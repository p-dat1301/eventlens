"""PostgreSQL/pgvector retrieval reads (lexical FTS, exact cosine, final check).

Every public method is exactly one read-only statement on its own short
connection, so no transaction is held across embedding or rerank calls and
query counts are bounded independently of the number of articles. Storage
failures become ``RetrievalStorageError``; they are never empty results.
"""

# SQL is assembled only from module-level literal constants; values are bound.
# ruff: noqa: S608

from collections.abc import Sequence
from datetime import datetime
from typing import LiteralString, final
from unicodedata import normalize
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine

from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    MetadataEligibility,
    RetrievalScope,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.retrieval import (
    ChunkRef,
    LexicalChunk,
    LexicalLimits,
    LexicalPage,
    RetrievalStorageError,
    VectorHit,
)
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import Row, execute, rows, transaction
from backend.shared.contracts import ArticleEnrichment, EnrichmentStatus
from backend.shared.contracts.taxonomy import TopicTag

# Source/time/metadata-eligibility filters, identical for both branches.
SCOPE_FILTER: LiteralString = """
    (cardinality(%(domains)s::text[]) = 0
        OR d.document_json->>'source_domain' = ANY(%(domains)s::text[]))
    AND (%(after)s::timestamptz IS NULL
        OR d.source_published_at >= %(after)s::timestamptz)
    AND (%(before)s::timestamptz IS NULL
        OR d.source_published_at < %(before)s::timestamptz)
    AND (NOT %(restrict)s OR d.article_id = ANY(%(ids)s::uuid[]))"""

# One READY publication of the current revision for its active generation,
# written by the job's current claim. History rows and superseded jobs never
# qualify; more than one qualifying row is treated as unavailable.
VALID_METADATA: LiteralString = """
    SELECT cur.article_id, p.metadata, p.index_generation
    FROM rag_article_current cur
    JOIN rag_enrichment_jobs job
        ON job.article_id = cur.article_id AND job.content_hash = cur.content_hash
        AND job.status = 'READY'
    JOIN rag_enrichment_publications p
        ON p.job_id = job.job_id AND p.claim_generation = job.claim_generation
        AND p.article_id = cur.article_id AND p.content_hash = cur.content_hash
        AND p.index_generation = cur.active_index_generation
        AND p.enrichment_version = job.enrichment_version
        AND p.model_version = job.model_version
        AND p.prompt_version = job.prompt_version
        AND p.metadata->>'status' = 'READY'
    JOIN rag_index_generations g
        ON g.generation = p.index_generation AND g.status = 'ACTIVE'
    WHERE cur.tombstoned_at IS NULL"""

ELIGIBILITY_SQL: LiteralString = (
    """
WITH scoped AS MATERIALIZED (
    SELECT d.article_id FROM rag_current_lexical d WHERE """
    + SCOPE_FILTER
    + """
), valid AS (
"""
    + VALID_METADATA
    + """
    AND cur.article_id IN (SELECT article_id FROM scoped)
), single AS (
    SELECT article_id, (array_agg(metadata))[1] AS metadata
    FROM valid GROUP BY article_id HAVING count(*) = 1
), matching AS (
    SELECT s.article_id FROM single s
    WHERE (cardinality(%(topics)s::text[]) = 0
           OR s.metadata->'topics' ?| %(topics)s::text[])
      AND (cardinality(%(entities)s::text[]) = 0 OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(s.metadata->'entities') entity
           WHERE entity->>'entity_id' = ANY(%(entities)s::text[])))
    ORDER BY s.article_id
    LIMIT %(cap)s + 1
)
SELECT (SELECT count(*) FROM scoped),
       (SELECT count(*) FROM single),
       (SELECT coalesce(array_agg(article_id::text ORDER BY article_id), '{}')
        FROM matching)
"""
)

# Lexical branch (policy v2): query lexemes come from the same 'simple'
# configuration as the stored search_vector (R3). Chunk windows replicate the
# T3 chunker arithmetic in SQL, are scanned up to a frozen per-article cap and
# capped again as a whole pool (R4); full chunk lists are never materialized.
LEXICAL_SQL: LiteralString = (
    """
WITH q AS MATERIALIZED (
    SELECT coalesce(array_agg(t.lexeme ORDER BY t.positions[1], t.lexeme), '{}')
        AS lexemes
    FROM (
        SELECT v.lexeme, v.positions
        FROM unnest(to_tsvector('simple', %(query)s)) v
        WHERE v.lexeme <> ALL(%(stopwords)s::text[])
        ORDER BY v.positions[1], v.lexeme
        LIMIT %(max_terms)s
    ) t
), tsq AS MATERIALIZED (
    SELECT q.lexemes,
           CASE WHEN cardinality(q.lexemes) = 0 THEN NULL
           ELSE array_to_string(ARRAY(
               SELECT quote_literal(l) FROM unnest(q.lexemes) l), ' | ')::tsquery
           END AS query
    FROM q
), docs AS MATERIALIZED (
    SELECT d.article_id, d.content_hash, d.title, d.source_published_at,
           d.normalized_content, char_length(d.normalized_content) AS len,
           ts_rank_cd(d.search_vector, tsq.query)::float8 AS rank
    FROM rag_current_lexical d, tsq
    WHERE tsq.query IS NOT NULL AND d.document_json IS NOT NULL
      AND d.search_vector @@ tsq.query AND """
    + SCOPE_FILTER
    + """
    ORDER BY rank DESC, d.source_published_at DESC NULLS LAST, d.article_id ASC
    LIMIT %(article_pool)s
), windows AS (
    SELECT docs.*,
           GREATEST(1, ceil(GREATEST(docs.len - %(size)s, 0)::numeric / %(step)s)::int
               + 1) AS total_chunks
    FROM docs
), chunks AS (
    SELECT w.article_id, w.content_hash, w.title, w.source_published_at, w.rank,
           i AS chunk_index, i * %(step)s AS start_offset,
           LEAST(i * %(step)s + %(size)s, w.len) AS end_offset,
           substr(w.normalized_content, i * %(step)s + 1,
                  LEAST(i * %(step)s + %(size)s, w.len) - i * %(step)s) AS content
    FROM windows w,
         generate_series(0, LEAST(%(scan)s, w.total_chunks) - 1) i
), scored AS (
    SELECT c.*, cardinality(ARRAY(
        SELECT unnest(tsvector_to_array(
            to_tsvector('simple', c.title || ' ' || c.content)))
        INTERSECT SELECT unnest(tsq.lexemes))) AS matched
    FROM chunks c, tsq
), ranked AS (
    -- R6: rank() keeps every window tied at the per-article cut. Within one
    -- article only `matched` varies before the frozen chunk_id tie-break, and
    -- chunk_id (UUIDv5) is applied in Python, so no eligible window is dropped.
    SELECT s.*, rank() OVER (
        PARTITION BY s.article_id ORDER BY s.matched DESC
    ) AS article_rank
    FROM scored s WHERE s.matched > 0
), pool AS (
    -- v3 bound: never more than max_metadata_candidates (+1 to detect breach;
    -- with v3 values it equals article_pool x scan cap and cannot bind).
    SELECT * FROM ranked WHERE article_rank <= %(per_article)s
    ORDER BY article_id ASC, chunk_index ASC
    LIMIT %(metadata_cap)s + 1
)
SELECT (SELECT cardinality(lexemes) FROM q),
       (SELECT count(*) FROM windows WHERE total_chunks > %(scan)s),
       p.article_id::text, p.content_hash, p.source_published_at, p.chunk_index,
       p.start_offset, p.end_offset, p.matched, p.rank
FROM (SELECT 1) one
LEFT JOIN pool p ON true
ORDER BY p.article_id ASC, p.chunk_index ASC
"""
)

# Exact route: distance is computed in a MATERIALIZED CTE and sorted outside
# it, so the HNSW index can never turn this into an approximate scan.
ACTIVE_GENERATIONS: LiteralString = """
WITH complete AS (
    SELECT cur.article_id, cur.content_hash, g.generation
    FROM rag_article_current cur
    JOIN rag_index_generations g ON g.generation = cur.active_index_generation
    WHERE cur.tombstoned_at IS NULL AND g.status = 'ACTIVE'
      AND g.article_id = cur.article_id AND g.content_hash = cur.content_hash
      AND g.embedding_model = %(model)s AND g.embedding_version = %(version)s
      AND g.embedding_dimension = %(dimension)s
      AND g.chunker_version = %(chunker)s
      AND g.normalization_version = %(normalization)s
      AND g.corpus_generation = %(corpus)s
      AND g.expected_chunks > 0
      AND g.expected_chunks = (
          SELECT count(*) FROM rag_chunks x
          WHERE x.article_id = cur.article_id AND x.content_hash = cur.content_hash
            AND x.generation = g.generation)
)"""

VECTOR_EXACT_SQL: LiteralString = (
    ACTIVE_GENERATIONS
    + """, scored AS MATERIALIZED (
    SELECT v.article_id, v.content_hash, v.generation, v.chunk_index,
           v.start_offset, v.end_offset, v.content, d.source_published_at,
           (v.embedding <=> %(embedding)s::vector(1536))::float8 AS distance
    FROM rag_current_vectors v
    JOIN complete a ON a.generation = v.generation
        AND a.article_id = v.article_id AND a.content_hash = v.content_hash
    JOIN rag_article_documents d
        ON d.article_id = v.article_id AND d.content_hash = v.content_hash
    WHERE """
    + SCOPE_FILTER
    + """
)
SELECT article_id::text, content_hash, generation, chunk_index, start_offset,
       end_offset, content, source_published_at, distance
FROM scored
ORDER BY distance ASC, article_id ASC, chunk_index ASC
LIMIT %(limit)s
"""
)

CHUNK_TEXT_SQL: LiteralString = """
SELECT k.ord::int, substr(d.normalized_content, k.start + 1, k.stop - k.start)
FROM unnest(%(ids)s::uuid[], %(hashes)s::text[], %(starts)s::int[],
            %(stops)s::int[]) WITH ORDINALITY AS k(article_id, content_hash, start,
                                                  stop, ord)
JOIN rag_article_documents d
    ON d.article_id = k.article_id AND d.content_hash = k.content_hash
ORDER BY k.ord
"""

CURRENT_SQL: LiteralString = (
    """
WITH valid AS (
"""
    + VALID_METADATA
    + """
    AND cur.article_id = ANY(%(ids)s::uuid[])
), agg AS (
    SELECT article_id, count(*) AS publications,
           (array_agg(metadata::text))[1] AS metadata,
           (array_agg(index_generation))[1] AS generation
    FROM valid GROUP BY article_id
)
SELECT d.document_json::text, cur.active_index_generation,
       CASE WHEN agg.publications = 1 THEN agg.metadata END,
       CASE WHEN agg.publications = 1 THEN agg.generation END
FROM rag_article_current cur
JOIN rag_article_documents d
    ON d.article_id = cur.article_id AND d.content_hash = cur.content_hash
LEFT JOIN agg ON agg.article_id = cur.article_id
WHERE cur.article_id = ANY(%(ids)s::uuid[]) AND cur.tombstoned_at IS NULL
  AND d.document_json IS NOT NULL
"""
)

_ELIGIBILITY_ROW = TypeAdapter(tuple[int, int, list[str]])
_LEXICAL_ROWS = TypeAdapter(
    list[
        tuple[
            int,
            int,
            str | None,
            str | None,
            datetime | None,
            int | None,
            int | None,
            int | None,
            int | None,
            float | None,
        ]
    ]
)
_TEXT_ROWS = TypeAdapter(list[tuple[int, str]])
_VECTOR_ROWS = TypeAdapter(
    list[tuple[str, str, int, int, int, int, str, datetime | None, float]]
)
_CURRENT_ROWS = TypeAdapter(list[tuple[str, int | None, str | None, int | None]])


def scope_params(scope: RetrievalScope) -> dict[str, object]:
    """Bind the shared scope filter."""
    return {
        "domains": list(scope.source_domains),
        "after": scope.published_after,
        "before": scope.published_before,
        "restrict": scope.allowed_article_ids is not None,
        "ids": list(scope.allowed_article_ids or ()),
    }


def identity_params(identity: EmbeddingIdentity) -> dict[str, object]:
    """Bind every embedding-identity component (not just the dimension)."""
    return {
        "model": identity.model,
        "version": identity.version,
        "dimension": identity.dimension,
        "chunker": identity.chunker_version,
        "normalization": identity.normalization_version,
        "corpus": identity.corpus_generation,
    }


def vector_literal(values: Sequence[float]) -> str:
    """Return the pgvector text literal."""
    return "[" + ",".join(map(repr, values)) + "]"


def _document(text: str, content_hash: str | None = None) -> ArticleDocument:
    try:
        document = ArticleDocument.model_validate_json(text)
    except ValidationError as error:
        raise RetrievalStorageError from error
    if content_hash is not None and document.content_hash != content_hash:
        raise RetrievalStorageError
    return document


def _metadata(text: str | None, document: ArticleDocument) -> ArticleEnrichment | None:
    """Fail closed: invalid or mismatching metadata is simply unavailable."""
    if text is None:
        return None
    try:
        metadata = ArticleEnrichment.model_validate_json(text)
    except ValidationError:
        return None
    if (
        metadata.status is not EnrichmentStatus.READY
        or metadata.article_id != document.article_id
        or metadata.content_hash != document.content_hash
    ):
        return None
    return metadata


@final
class PostgresRetrievalStore:
    """Read-only, batched retrieval storage on PostgreSQL + pgvector."""

    def __init__(self, engine: Engine) -> None:
        """Use the process Engine; ``statements`` counts reads."""
        self._engine = engine
        self.statements = 0

    def _fetch(self, query: LiteralString, params: dict[str, object]) -> list[Row]:
        try:
            with transaction(self._engine, read_only=True) as connection:
                self.statements += 1
                return rows(execute(connection, query, params))
        except (StorageUnavailableError, StorageOperationError) as error:
            raise RetrievalStorageError from error

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[TopicTag, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        """Count scoped articles and list valid-metadata matches (one statement)."""
        rows = self._fetch(
            ELIGIBILITY_SQL,
            {
                **scope_params(scope),
                "topics": [str(topic) for topic in topics],
                "entities": [str(entity) for entity in entity_ids],
                "cap": scan_cap,
            },
        )
        scoped, verifiable, matching = _ELIGIBILITY_ROW.validate_python(rows[0])
        return MetadataEligibility(
            scoped_articles=scoped,
            verifiable_articles=verifiable,
            matching_article_ids=tuple(UUID(item) for item in matching[:scan_cap]),
            scan_cap_reached=len(matching) > scan_cap,
        )

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Bounded lexical chunk pool in one statement (see ``LEXICAL_SQL``)."""
        rows = _LEXICAL_ROWS.validate_python(
            self._fetch(
                LEXICAL_SQL,
                {
                    **scope_params(scope),
                    "query": normalize("NFC", query),
                    "stopwords": list(limits.stopwords),
                    "max_terms": limits.max_terms,
                    "article_pool": limits.article_pool,
                    "scan": limits.chunks_scanned_per_article,
                    "per_article": limits.chunks_per_article,
                    "metadata_cap": limits.max_metadata_candidates,
                    "size": limits.chunk_size,
                    "step": limits.chunk_size - limits.chunk_overlap,
                },
            )
        )
        term_count, truncated = rows[0][0], rows[0][1]
        chunks = tuple(
            LexicalChunk(
                article_id=UUID(article_id),
                content_hash=content_hash,
                source_published_at=published,
                chunk_index=index,
                start_offset=start,
                end_offset=end,
                matched_terms=matched,
                document_rank=rank,
            )
            for (
                _,
                _,
                article_id,
                content_hash,
                published,
                index,
                start,
                end,
                matched,
                rank,
            ) in rows
            if article_id is not None
            and content_hash is not None
            and index is not None
            and start is not None
            and end is not None
            and matched is not None
            and rank is not None
        )
        return LexicalPage(
            term_count=term_count, chunks=chunks, truncated_articles=truncated
        )

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        """Immutable revision slices for the selected chunks (one statement)."""
        if not chunks:
            return ()
        rows = _TEXT_ROWS.validate_python(
            self._fetch(
                CHUNK_TEXT_SQL,
                {
                    "ids": [chunk.article_id for chunk in chunks],
                    "hashes": [chunk.content_hash for chunk in chunks],
                    "starts": [chunk.start_offset for chunk in chunks],
                    "stops": [chunk.end_offset for chunk in chunks],
                },
            )
        )
        if [ordinal for ordinal, _ in rows] != list(range(1, len(chunks) + 1)):
            raise RetrievalStorageError
        return tuple(text for _, text in rows)

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        """Filtered exact cosine search over active complete generations."""
        rows = _VECTOR_ROWS.validate_python(
            self._fetch(
                VECTOR_EXACT_SQL,
                {
                    **scope_params(scope),
                    **identity_params(identity),
                    "embedding": vector_literal(embedding),
                    "limit": limit,
                },
            )
        )
        return tuple(
            VectorHit(
                article_id=UUID(article_id),
                content_hash=content_hash,
                generation=generation,
                chunk_index=chunk_index,
                start_offset=start,
                end_offset=end,
                content=content,
                source_published_at=published,
                distance=distance,
            )
            for (
                article_id,
                content_hash,
                generation,
                chunk_index,
                start,
                end,
                content,
                published,
                distance,
            ) in rows
        )

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        """Batched final check: current documents plus valid READY metadata."""
        if not article_ids:
            return ()
        rows = _CURRENT_ROWS.validate_python(
            self._fetch(CURRENT_SQL, {"ids": list(article_ids)})
        )
        states: list[CurrentArticleState] = []
        for text, active, metadata_text, metadata_generation in rows:
            document = _document(text)
            metadata = _metadata(metadata_text, document)
            states.append(
                CurrentArticleState(
                    document=document,
                    active_index_generation=active,
                    metadata=metadata,
                    metadata_generation=(
                        None if metadata is None else metadata_generation
                    ),
                )
            )
        return tuple(states)
