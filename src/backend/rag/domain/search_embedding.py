"""Identity and checks for search-index embeddings (BGE-M3 baseline).

The PostgreSQL vector schema (``vector(1536)``, fake embeddings) is untouched.
Search embeddings live in their own generation, identified by a model identity
string such as ``bge-m3-1024.v1``; a vector is accepted only with the exact
identity and dimension of the generation it is written to or queried against.
"""

import math
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

BGE_M3_MODEL: Final = "BAAI/bge-m3"
BGE_M3_IDENTITY: Final = "bge-m3-1024.v1"
BGE_M3_DIMENSIONS: Final = 1024
FAKE_BGE_M3_IDENTITY: Final = "fake-bge-m3-1024.v1"
# What the model sees for a chunk: title line, blank line, exact chunk text.
EMBEDDING_INPUT_FORMAT: Final = "title-blank-chunk.v1"


class EmbeddingDimensionMismatchError(ValueError):
    """A vector does not have the generation's dimension."""

    code: Final = "embedding_dimension_mismatch"

    def __init__(self, expected: int, actual: int) -> None:
        """Keep both sizes; never the vector values."""
        super().__init__(f"embedding dimension {actual} != expected {expected}")


class EmbeddingIdentityMismatchError(ValueError):
    """A vector or generation was produced by a different model identity."""

    code: Final = "embedding_identity_mismatch"

    def __init__(self, expected: str, actual: str) -> None:
        """Keep both identities."""
        super().__init__(f"embedding identity {actual!r} != expected {expected!r}")


class InvalidEmbeddingError(ValueError):
    """Non-finite or all-zero vectors are never indexed or searched."""

    code: Final = "embedding_invalid"

    def __init__(self) -> None:
        """Constant message: values are never echoed."""
        super().__init__("embedding has non-finite values or zero norm")


@dataclass(frozen=True, slots=True)
class SearchEmbeddingIdentity:
    """Embedding space recorded on every search generation, cache row and query.

    ``identity`` names the family (``bge-m3-1024.v1``); ``revision`` pins the
    model snapshot (empty for deterministic fakes). Everything that changes
    the vectors - model, revision, dimensions, token limit, pooling, output
    normalization and the input format - goes into ``key``, the fingerprint
    stored as the generation's embedding identity and used as the cache key,
    so two snapshots of one model never share vectors.
    """

    identity: str
    model: str
    dimensions: int
    revision: str = ""
    max_tokens: int = 8192
    pooling: str = "cls"
    normalization: str = "l2"
    input_format: str = EMBEDDING_INPUT_FORMAT

    def __post_init__(self) -> None:
        """Reject impossible dimensions early."""
        if self.dimensions <= 0:
            raise EmbeddingDimensionMismatchError(1, self.dimensions)

    @property
    def key(self) -> str:
        """Full embedding-space fingerprint: ``identity@revision:sha256[:16]``."""
        material = "|".join(
            (
                self.identity,
                self.model,
                self.revision,
                str(self.dimensions),
                str(self.max_tokens),
                self.pooling,
                self.normalization,
                self.input_format,
            )
        )
        digest = sha256(material.encode()).hexdigest()[:16]
        return f"{self.identity}@{self.revision or '-'}:{digest}"

    def require(self, other: "SearchEmbeddingIdentity") -> None:
        """Fail unless ``other`` is the same embedding space and dimension."""
        if other.dimensions != self.dimensions:
            raise EmbeddingDimensionMismatchError(self.dimensions, other.dimensions)
        if other.key != self.key:
            raise EmbeddingIdentityMismatchError(self.key, other.key)

    def require_key(self, key: str | None, dimensions: int | None) -> None:
        """Fail unless a stored generation key/dimension matches this space."""
        if dimensions is not None and dimensions != self.dimensions:
            raise EmbeddingDimensionMismatchError(dimensions, self.dimensions)
        if key != self.key:
            raise EmbeddingIdentityMismatchError(str(key), self.key)


BGE_M3: Final = SearchEmbeddingIdentity(
    identity=BGE_M3_IDENTITY, model=BGE_M3_MODEL, dimensions=BGE_M3_DIMENSIONS
)
FAKE_BGE_M3: Final = SearchEmbeddingIdentity(
    identity=FAKE_BGE_M3_IDENTITY,
    model="fixture-bge-m3",
    dimensions=BGE_M3_DIMENSIONS,
    pooling="feature-hashing",
)


def checked_vector(values: tuple[float, ...], dimensions: int) -> tuple[float, ...]:
    """Return ``values`` if it has ``dimensions`` finite, non-zero components."""
    if len(values) != dimensions:
        raise EmbeddingDimensionMismatchError(dimensions, len(values))
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise InvalidEmbeddingError
    return values


def l2_normalize(values: tuple[float, ...]) -> tuple[float, ...]:
    """Unit-length copy (cosine == dot product afterwards)."""
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0 or not math.isfinite(norm):
        raise InvalidEmbeddingError
    return tuple(value / norm for value in values)
