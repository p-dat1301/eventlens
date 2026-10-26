"""Immutable vector generation identity."""

from dataclasses import dataclass
from typing import Final

BASELINE_DIMENSION: Final = 1536


class UnsupportedDimensionError(ValueError):
    """Dimension changes require an approved database migration."""

    def __init__(self, dimension: int) -> None:
        """Retain unsupported dimension in diagnostic text."""
        super().__init__(f"unsupported embedding dimension: {dimension}")


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """Identity required to distinguish compatible fake vector generations."""

    model: str = "fixture-embedding"
    version: str = "fixture-embedding.v1"
    dimension: int = 1536
    chunker_version: str = "codepoint-1200-200.v1"
    normalization_version: str = "nfc.v1"
    corpus_generation: str = "rag-fixture-corpus.v1"

    def __post_init__(self) -> None:
        """Reject dimensions unsupported by the fixed vector schema."""
        if self.dimension != BASELINE_DIMENSION:
            raise UnsupportedDimensionError(self.dimension)


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Versioned fixed-dimension embedding result."""

    model: str
    version: str
    values: tuple[float, ...]
