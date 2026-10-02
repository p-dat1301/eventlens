"""Transactional vector staging and generation recovery."""

from dataclasses import dataclass
from uuid import uuid4

import psycopg
from psycopg.rows import scalar_row
from pydantic import TypeAdapter

from eventlens_rag.domain.staging import StageRequest


@dataclass(frozen=True, slots=True)
class PostgresStaging:
    """Stage complete generations idempotently, never expose incomplete chunks."""

    database_url: str

    def stage(self, request: StageRequest) -> int:
        """Retry interrupted writes using the complete immutable generation key."""
        article = request.article
        identity = request.identity
        with psycopg.connect(self.database_url) as connection:
            row = (
                connection.cursor(row_factory=scalar_row)
                .execute(
                    """INSERT INTO rag_index_generations (
                    article_id, content_hash, embedding_model, embedding_version,
                    embedding_dimension, chunker_version, normalization_version,
                    corpus_generation, expected_chunks
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (article_id, content_hash, normalization_version,
                    chunker_version, embedding_model, embedding_version,
                    embedding_dimension, corpus_generation)
                DO UPDATE SET expected_chunks = rag_index_generations.expected_chunks
                RETURNING generation""",
                    (
                        article.article_id,
                        article.content_hash,
                        identity.model,
                        identity.version,
                        identity.dimension,
                        identity.chunker_version,
                        identity.normalization_version,
                        identity.corpus_generation,
                        len(request.chunks),
                    ),
                )
                .fetchone()
            )
            generation = TypeAdapter(int).validate_python(row)
            for chunk, vector in zip(request.chunks, request.vectors, strict=True):
                _ = connection.execute(
                    """INSERT INTO rag_chunks (
                        chunk_id, article_id, content_hash, generation, chunk_index,
                        start_offset, end_offset, content, fingerprint, embedding
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector(1536))
                    ON CONFLICT (article_id, content_hash, generation, chunk_index)
                    DO NOTHING""",
                    (
                        uuid4(),
                        article.article_id,
                        article.content_hash,
                        generation,
                        chunk.index,
                        chunk.start_offset,
                        chunk.end_offset,
                        chunk.content,
                        chunk.fingerprint,
                        "[" + ",".join(map(str, vector.values)) + "]",
                    ),
                )
            return generation

    def activate(self, generation: int) -> bool:
        """Publish a complete fake rebuild only against its current article revision."""
        with psycopg.connect(self.database_url) as connection:
            row = connection.execute(
                """SELECT current.article_id FROM rag_article_current current
                JOIN rag_index_generations staged USING (article_id, content_hash)
                WHERE staged.generation = %s AND current.tombstoned_at IS NULL
                AND staged.expected_chunks > 0
                AND staged.expected_chunks = (SELECT count(*) FROM rag_chunks
                    WHERE generation = staged.generation)
                FOR UPDATE OF current, staged""",
                (generation,),
            ).fetchone()
            if row is None:
                return False
            _ = connection.execute(
                """UPDATE rag_index_generations SET status = 'ACTIVE'
                WHERE generation = %s""",
                (generation,),
            )
            _ = connection.execute(
                """UPDATE rag_article_current SET active_index_generation = %s
                WHERE article_id = (SELECT article_id FROM rag_index_generations
                    WHERE generation = %s)""",
                (generation, generation),
            )
            return True
