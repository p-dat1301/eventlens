"""Project one PostgreSQL revision into search-index chunk documents.

Chunks come from the chunker of the generation (``paragraph-900-1.v1`` by
default; the baseline ``codepoint-1200-200.v1`` for chunker comparisons, both
exact code-point slices that the PostgreSQL final check recomputes); vectors come from
the embedding cache keyed by ``(text_hash, embedding identity)``, so an
unchanged revision is never embedded twice. The embedder's identity must equal
the generation's identity exactly, otherwise nothing is projected.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.search_sync import EmbeddingCache, ProjectionRevision
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import ChunkingPolicy, chunk_bounds, chunk_document
from backend.rag.domain.paragraph_chunking import (
    DEFAULT_PARAGRAPH_POLICY,
    ChunkMetadata,
    ParagraphChunk,
    ParagraphChunkingPolicy,
    chunk_paragraphs,
    chunk_spans,
    paragraph_spans,
)
from backend.rag.domain.search_embedding import (
    EmbeddingIdentityMismatchError,
    SearchEmbeddingIdentity,
    checked_vector,
)
from backend.rag.domain.search_index import (
    IndexGeneration,
    SearchChunkDocument,
    revision_id_for,
    scopes_for_visibility,
    search_chunk_id,
    text_hash,
)

SAPO_MAX_CHARS = 400

ProjectionChunker = ParagraphChunkingPolicy | ChunkingPolicy


class ChunkerMismatchError(ValueError):
    """The generation was built with a different chunker version."""

    def __init__(self, expected: str, actual: str) -> None:
        """Keep both versions."""
        super().__init__(f"chunker {actual!r} != generation chunker {expected!r}")


@dataclass(slots=True)
class ProjectionStats:
    """Counters across one projection run (no text)."""

    revisions: int = 0
    chunks: int = 0
    embedded: int = 0
    cache_hits: int = 0
    embedding_batches: int = 0


def embedding_input(chunk: ParagraphChunk) -> str:
    """Text the model sees: title line, then the exact chunk text."""
    title = "" if chunk.metadata is None else chunk.metadata.title
    return f"{title}\n\n{chunk.content}" if title else chunk.content


def sapo_of(text: str) -> str:
    """Lead paragraph used as the ``sapo`` field (ArticleDocument has none)."""
    spans = paragraph_spans(text)
    if not spans:
        return ""
    start, end = spans[0]
    return text[start : min(end, start + SAPO_MAX_CHARS)]


def chunk_count(text: str, policy: ProjectionChunker) -> int:
    """Chunks the generation's chunker makes of ``text`` (reconcile check)."""
    if isinstance(policy, ParagraphChunkingPolicy):
        return len(chunk_spans(text, policy))
    count = 0
    while chunk_bounds(len(text), count, policy) is not None:
        count += 1
    return count


def _paragraph_of(starts: Sequence[int], offset: int) -> int:
    return max(bisect_right(starts, offset) - 1, 0)


def chunk_revision(
    document: ArticleDocument,
    metadata: ChunkMetadata | None,
    policy: ProjectionChunker,
) -> tuple[ParagraphChunk, ...]:
    """Chunks of one revision with the generation's chunker.

    Codepoint windows keep their baseline offsets and fingerprints; the
    paragraph indexes are only provenance (the paragraphs the window touches).
    """
    if isinstance(policy, ParagraphChunkingPolicy):
        return chunk_paragraphs(
            document.article_id,
            document.content_hash,
            document.normalized_content,
            metadata=metadata,
            policy=policy,
        )
    starts = [start for start, _ in paragraph_spans(document.normalized_content)]
    return tuple(
        ParagraphChunk(
            article_id=document.article_id,
            content_hash=document.content_hash,
            chunker_version=policy.version,
            index=chunk.index,
            char_start=chunk.start_offset,
            char_end=chunk.end_offset,
            paragraph_start=_paragraph_of(starts, chunk.start_offset),
            paragraph_end=_paragraph_of(starts, max(chunk.end_offset - 1, 0)),
            content=chunk.content,
            fingerprint=chunk.fingerprint,
            metadata=metadata,
        )
        for chunk in chunk_document(document, policy)
    )


@dataclass(slots=True)
class ProjectRevision:
    """Chunk + embed (cache first) one revision for one generation."""

    embedder: BatchEmbeddingPort | None
    cache: EmbeddingCache | None
    policy: ProjectionChunker = DEFAULT_PARAGRAPH_POLICY
    batch_size: int = 16
    stats: ProjectionStats = field(default_factory=ProjectionStats)

    def _identity(self, generation: IndexGeneration) -> SearchEmbeddingIdentity | None:
        if generation.embedding_identity is None:
            return None
        if self.embedder is None:
            raise EmbeddingIdentityMismatchError(generation.embedding_identity, "none")
        identity = self.embedder.identity
        # The generation stores the full embedding-space fingerprint (model,
        # revision, dimensions, token limit, pooling, normalization, input).
        identity.require_key(
            generation.embedding_identity, generation.embedding_dimensions
        )
        return identity

    def execute(
        self, revision: ProjectionRevision, generation: IndexGeneration
    ) -> tuple[SearchChunkDocument, ...]:
        """Chunk documents for the revision, or ``()`` when nothing is current."""
        return self.execute_many((revision,), generation)

    def execute_many(
        self, revisions: Sequence[ProjectionRevision], generation: IndexGeneration
    ) -> tuple[SearchChunkDocument, ...]:
        """Batched projection (one cache lookup and embedding pass per call)."""
        if generation.chunk_version != self.policy.version:
            raise ChunkerMismatchError(generation.chunk_version, self.policy.version)
        identity = self._identity(generation)
        planned: list[tuple[ProjectionRevision, tuple[ParagraphChunk, ...]]] = []
        for revision in revisions:
            document = revision.document
            if document is None or revision.tombstoned:
                continue
            metadata = ChunkMetadata(
                title=document.title,
                source_name=document.source_name,
                source_domain=document.source_domain,
                published_at=document.source_published_at,
            )
            chunks = chunk_revision(document, metadata, self.policy)
            planned.append((revision, chunks))
        vectors = (
            {}
            if identity is None
            else self._vectors(
                [chunk for _, chunks in planned for chunk in chunks], identity
            )
        )
        documents: list[SearchChunkDocument] = []
        for revision, chunks in planned:
            documents.extend(self._documents(revision, chunks, generation, vectors))
            self.stats.revisions += 1
            self.stats.chunks += len(chunks)
        return tuple(documents)

    def _vectors(
        self, chunks: Sequence[ParagraphChunk], identity: SearchEmbeddingIdentity
    ) -> dict[str, tuple[float, ...]]:
        embedder = self.embedder
        assert embedder is not None  # noqa: S101 - checked by _identity
        inputs = {
            text_hash(embedding_input(chunk)): embedding_input(chunk)
            for chunk in chunks
        }
        found = (
            {} if self.cache is None else self.cache.get_many(list(inputs), identity)
        )
        self.stats.cache_hits += len(found)
        missing = [key for key in inputs if key not in found]
        fresh: list[tuple[str, tuple[float, ...]]] = []
        for start in range(0, len(missing), self.batch_size):
            keys = missing[start : start + self.batch_size]
            self.stats.embedding_batches += 1
            values = embedder.embed_documents([inputs[key] for key in keys])
            if len(values) != len(keys):
                raise EmbeddingIdentityMismatchError(identity.key, "short_batch")
            fresh.extend(
                (key, checked_vector(value, identity.dimensions))
                for key, value in zip(keys, values, strict=True)
            )
        self.stats.embedded += len(fresh)
        if fresh and self.cache is not None:
            _ = self.cache.put_many(fresh, identity)
        return {**found, **dict(fresh)}

    @staticmethod
    def _documents(
        revision: ProjectionRevision,
        chunks: tuple[ParagraphChunk, ...],
        generation: IndexGeneration,
        vectors: dict[str, tuple[float, ...]],
    ) -> list[SearchChunkDocument]:
        document = revision.document
        assert document is not None  # noqa: S101 - filtered by caller
        metadata = revision.metadata
        topics = () if metadata is None else tuple(str(t) for t in metadata.topics)
        entities = (
            ()
            if metadata is None
            else tuple(
                sorted(
                    str(entity.entity_id)
                    for entity in metadata.entities
                    if entity.entity_id is not None
                )
            )
        )
        sapo = sapo_of(document.normalized_content)
        revision_id = revision_id_for(document.article_id, document.content_hash)
        scopes = scopes_for_visibility(document.visibility)
        out: list[SearchChunkDocument] = []
        for chunk in chunks:
            vector = vectors.get(text_hash(embedding_input(chunk)))
            out.append(
                SearchChunkDocument(
                    article_id=document.article_id,
                    revision_id=revision_id,
                    chunk_id=search_chunk_id(
                        document.article_id,
                        document.content_hash,
                        chunk.chunker_version,
                        chunk.index,
                    ),
                    content_hash=document.content_hash,
                    chunk_version=chunk.chunker_version,
                    chunk_index=chunk.index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    paragraph_start=chunk.paragraph_start,
                    paragraph_end=chunk.paragraph_end,
                    title=document.title,
                    sapo=sapo,
                    text=chunk.content,
                    source_name=document.source_name,
                    source_domain=document.source_domain,
                    published_at=document.source_published_at,
                    topics=topics,
                    entity_ids=entities,
                    embedding=vector,
                    embedding_model=generation.embedding_model,
                    embedding_identity=generation.embedding_identity,
                    embedding_dimensions=generation.embedding_dimensions,
                    generation_id=generation.generation_id,
                    access_scopes=scopes,
                    projection_version=revision.version,
                )
            )
        return out
