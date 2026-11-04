"""Engine-neutral chunk index values shared by search-selection adapters.

Every engine indexes the same T3 chunk windows (``ChunkRef``) of the same
current revisions, so results from PostgreSQL FTS, Elasticsearch BM25 and
pgvector are comparable chunk for chunk and map back to exact NFC code-point
offsets for citations.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from backend.rag.domain.retrieval import ChunkRef

# Lexical/semantic text of a chunk: its title, a blank line, then the exact
# chunk slice. Frozen for every engine so no engine sees more text than another.
CHUNK_TEXT_TEMPLATE = "{title}\n\n{content}"


class InvalidEmbeddingError(ValueError):
    """An embedder returned a vector of the wrong size or non-finite values."""

    code: ClassVar[str] = "embedding_invalid"

    def __init__(self, expected: int, actual: int) -> None:
        """Describe the shape problem without any vector values."""
        super().__init__(f"expected {expected} finite values, got {actual}")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk window plus the filter fields every engine indexes."""

    chunk: ChunkRef
    title: str
    content: str
    source_domain: str
    source_published_at: datetime | None

    @property
    def text(self) -> str:
        """Frozen engine input (``CHUNK_TEXT_TEMPLATE``)."""
        return CHUNK_TEXT_TEMPLATE.format(title=self.title, content=self.content)


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """Engine hit; ``score`` is engine-specific and only ordered within one run."""

    chunk: ChunkRef
    score: float
    source_published_at: datetime | None


@dataclass(frozen=True, slots=True)
class LexicalIndexIdentity:
    """Everything that changes lexical ranking; recorded in every report."""

    engine: str
    engine_version: str
    analyzer_id: str
    analyzer_digest: str
    similarity: str


@dataclass(frozen=True, slots=True)
class EmbedderIdentity:
    """Model, revision and input conventions of a real or fake embedder.

    Unlike the production ``EmbeddingIdentity`` (fixed 1536 schema), this
    identity accepts any dimension: evaluation vectors live in their own
    per-identity tables and never in ``rag_chunks``.
    """

    model: str
    revision: str
    dimension: int
    pooling: str
    query_prefix: str
    document_prefix: str
    max_tokens: int
    normalized: bool = True

    @property
    def slug(self) -> str:
        """Stable short name for tables and reports."""
        base = "".join(ch if ch.isalnum() else "_" for ch in self.model.lower())
        return f"{base}_{self.dimension}"[:48].strip("_")


def checked_vector(values: tuple[float, ...], dimension: int) -> tuple[float, ...]:
    """Reject wrong sizes and non-finite values; never pad or truncate."""
    if len(values) != dimension or not all(math.isfinite(v) for v in values):
        raise InvalidEmbeddingError(dimension, len(values))
    return values
