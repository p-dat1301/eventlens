"""Transactional vector staging and generation recovery."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import Engine

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.staging import StageRequest
from backend.rag.infra.postgres.engine import execute, scalar, scalars, transaction

_COMPLETE = """staged.expected_chunks > 0
    AND staged.expected_chunks = (SELECT count(*) FROM rag_chunks chunk
        WHERE chunk.generation = staged.generation)"""


class IncompleteGenerationError(RuntimeError):
    """A requested generation lacks its expected chunks; nothing was activated."""

    def __init__(self, generations: Sequence[int]) -> None:
        """Name the incomplete generations."""
        super().__init__(f"incomplete index generations: {list(generations)}")


class DuplicateArticleGenerationError(RuntimeError):
    """A batch must not assign two distinct generations to one article."""

    def __init__(self, article_ids: Sequence[str]) -> None:
        """Attach ambiguous article IDs to the batch error."""
        super().__init__(f"multiple generations for one article: {list(article_ids)}")


@dataclass(frozen=True, slots=True)
class PostgresStaging:
    """Stage complete generations idempotently, never expose incomplete chunks."""

    engine: Engine

    def find_complete(
        self, article: ArticleDocument, identity: EmbeddingIdentity
    ) -> int | None:
        """Return a reusable complete generation for this exact embedding key."""
        with transaction(self.engine) as connection:
            row = scalar(
                execute(
                    connection,
                    f"""SELECT staged.generation FROM rag_index_generations staged
                    WHERE staged.article_id = %s AND staged.content_hash = %s
                    AND staged.embedding_model = %s AND staged.embedding_version = %s
                    AND staged.embedding_dimension = %s
                    AND staged.chunker_version = %s
                    AND staged.normalization_version = %s
                    AND staged.corpus_generation = %s AND {_COMPLETE}""",  # noqa: S608
                    (
                        article.article_id,
                        article.content_hash,
                        identity.model,
                        identity.version,
                        identity.dimension,
                        identity.chunker_version,
                        identity.normalization_version,
                        identity.corpus_generation,
                    ),
                )
            )
        adapter: TypeAdapter[int | None] = TypeAdapter(int | None)
        return adapter.validate_python(row)

    def stage(self, request: StageRequest) -> int:
        """Write generation row and every chunk in one transaction.

        An interrupted stage rolls back entirely, so a retry starts clean and the
        immutable generation key makes repeated complete stages idempotent.
        """
        article = request.article
        identity = request.identity
        with transaction(self.engine) as connection:
            row = scalar(
                execute(
                    connection,
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
            )
            generation = TypeAdapter(int).validate_python(row)
            for chunk, vector in zip(request.chunks, request.vectors, strict=True):
                _ = execute(
                    connection,
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
        """Publish one complete generation only against its current revision."""
        try:
            return self.activate_all((generation,)) == 1
        except IncompleteGenerationError:
            return False

    def activate_all(self, generations: Sequence[int]) -> int:
        """Flip every eligible generation in one transaction or none at all.

        Incomplete generations abort the whole flip. Generations whose article
        was corrected or tombstoned after staging are skipped, never activated.
        """
        requested = list(generations)
        with transaction(self.engine) as connection:
            incomplete = TypeAdapter(list[int]).validate_python(
                scalars(
                    execute(
                        connection,
                        f"""SELECT staged.generation FROM rag_index_generations staged
                    WHERE staged.generation = ANY(%s) AND NOT ({_COMPLETE})""",  # noqa: S608
                        (requested,),
                    )
                )
            )
            if incomplete:
                raise IncompleteGenerationError(incomplete)
            eligible = TypeAdapter(list[int]).validate_python(
                scalars(
                    execute(
                        connection,
                        """SELECT staged.generation FROM rag_article_current current
                    JOIN rag_index_generations staged USING (article_id, content_hash)
                    WHERE staged.generation = ANY(%s)
                    AND current.tombstoned_at IS NULL
                    FOR UPDATE OF current, staged""",
                        (requested,),
                    )
                )
            )
            duplicates = TypeAdapter(list[str]).validate_python(
                scalars(
                    execute(
                        connection,
                        """SELECT article_id::text FROM rag_index_generations
                    WHERE generation = ANY(%s)
                    GROUP BY article_id HAVING count(*) > 1""",
                        (eligible,),
                    )
                )
            )
            if duplicates:
                raise DuplicateArticleGenerationError(duplicates)
            _ = execute(
                connection,
                """UPDATE rag_index_generations SET status = 'ACTIVE'
                WHERE generation = ANY(%s)""",
                (eligible,),
            )
            _ = execute(
                connection,
                """UPDATE rag_article_current current
                SET active_index_generation = staged.generation
                FROM rag_index_generations staged
                WHERE staged.generation = ANY(%s)
                AND current.article_id = staged.article_id""",
                (eligible,),
            )
            return len(eligible)
