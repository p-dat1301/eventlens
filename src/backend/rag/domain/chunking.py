"""Deterministic Unicode codepoint chunking."""

from dataclasses import dataclass
from hashlib import sha256

from backend.rag.domain.article_document import ArticleDocument


class InvalidChunkingError(ValueError):
    """Chunk windows must advance through normalized content."""

    def __init__(self, size: int, overlap: int) -> None:
        """Explain invalid window dimensions."""
        super().__init__(
            f"positive size required; overlap must be smaller: {size}/{overlap}"
        )


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """Codepoint chunking configuration."""

    size: int = 1200
    overlap: int = 200
    version: str = "codepoint-1200-200.v1"

    def __post_init__(self) -> None:
        """Enforce positive progress for every chunk."""
        if self.size <= 0 or self.overlap < 0 or self.overlap >= self.size:
            raise InvalidChunkingError(self.size, self.overlap)


@dataclass(frozen=True, slots=True)
class Chunk:
    """Immutable indexed codepoint range."""

    article_id: str
    content_hash: str
    index: int
    start_offset: int
    end_offset: int
    content: str
    fingerprint: str


DEFAULT_POLICY = ChunkingPolicy()


def chunk_document(
    document: ArticleDocument, policy: ChunkingPolicy = DEFAULT_POLICY
) -> tuple[Chunk, ...]:
    """Split normalized content into overlapping Unicode codepoint chunks."""
    step = policy.size - policy.overlap
    chunks: list[Chunk] = []
    for index, start in enumerate(range(0, len(document.normalized_content), step)):
        end = min(start + policy.size, len(document.normalized_content))
        content = document.normalized_content[start:end]
        fingerprint = sha256(
            f"{document.content_hash}:{policy.version}:{index}:{content}".encode()
        ).hexdigest()
        chunks.append(
            Chunk(
                article_id=str(document.article_id),
                content_hash=document.content_hash,
                index=index,
                start_offset=start,
                end_offset=end,
                content=content,
                fingerprint=f"sha256:{fingerprint}",
            )
        )
        if end == len(document.normalized_content):
            break
    return tuple(chunks)


def chunk_bounds(
    length: int, index: int, policy: ChunkingPolicy = DEFAULT_POLICY
) -> tuple[int, int] | None:
    """Offsets ``chunk_document`` gives chunk ``index`` of ``length`` code points.

    Pure arithmetic, so callers can validate one chunk without materializing
    every chunk of a long document. ``None`` when no such chunk exists.
    """
    step = policy.size - policy.overlap
    if index < 0 or length <= 0:
        return None
    if index > 0 and (index - 1) * step + policy.size >= length:
        return None
    start = index * step
    return start, min(start + policy.size, length)
