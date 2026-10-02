"""Validated staging input independent of database infrastructure."""

from dataclasses import dataclass
from math import isfinite

from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import Chunk, chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.ports.embeddings import EmbeddingVector


class InvalidStageError(ValueError):
    """Staged chunks or vectors do not represent the requested article identity."""


@dataclass(frozen=True, slots=True)
class StageRequest:
    """Complete immutable article vector generation ready for staging."""

    article: ArticleDocument
    identity: EmbeddingIdentity
    chunks: tuple[Chunk, ...]
    vectors: tuple[EmbeddingVector, ...]

    def __post_init__(self) -> None:
        """Validate exact chunks, finite vectors and matching embedding identity."""
        if (
            self.identity.normalization_version != self.article.normalization_version
            or self.identity.chunker_version != "codepoint-1200-200.v1"
        ):
            raise InvalidStageError(self.article.article_id)
        if self.chunks != chunk_document(self.article):
            raise InvalidStageError(self.article.article_id)
        if len(self.chunks) != len(self.vectors):
            raise InvalidStageError(self.article.article_id)
        for vector in self.vectors:
            if (
                vector.model != self.identity.model
                or vector.version != self.identity.version
                or len(vector.values) != self.identity.dimension
                or not all(isfinite(value) for value in vector.values)
                or not any(vector.values)
            ):
                raise InvalidStageError(self.article.article_id)
