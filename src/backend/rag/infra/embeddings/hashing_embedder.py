"""Deterministic hashing ``TextEmbedder`` of any dimension (tests/CI only).

Bag of lowercased word tokens hashed into signed buckets, L2-normalized. It
proves plumbing and contracts; it says nothing about semantic quality.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import blake2b

from backend.rag.domain.search_index import EmbedderIdentity

_TOKEN = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class HashingEmbedder:
    """Fake embedder; ``identity.model`` always starts with ``fake-``."""

    dimension: int = 64

    @property
    def identity(self) -> EmbedderIdentity:
        """Fake identity with the requested dimension."""
        return EmbedderIdentity(
            model="fake-hashing",
            revision="v1",
            dimension=self.dimension,
            pooling="token-hash-sum",
            query_prefix="",
            document_prefix="",
            max_tokens=10_000,
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        values = [0.0] * self.dimension
        tokens: list[str] = _TOKEN.findall(text.lower())
        for token in tokens:
            digest = blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            values[bucket] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        if norm == 1.0 and not any(values):
            values[0] = 1.0
        return tuple(v / norm for v in values)

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Hash each document."""
        return tuple(self._vector(text) for text in texts)

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Queries use the same function (no instruction)."""
        return tuple(self._vector(text) for text in texts)
