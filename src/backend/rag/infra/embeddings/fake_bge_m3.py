"""Deterministic 1024-dimension stand-in for BGE-M3 (unit tests, CI evals).

Feature hashing of casefolded word unigrams (plus accent-folded variants),
signed and L2-normalized. Texts sharing words get positive cosine, so hybrid
fusion mechanics can be tested without a model. It measures nothing about
BGE-M3 quality and its identity (``fake-bge-m3-1024.v1``) never matches a
real BGE-M3 generation.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import blake2b

from backend.rag.domain.search_embedding import (
    FAKE_BGE_M3,
    SearchEmbeddingIdentity,
    l2_normalize,
)

_WORD = re.compile(r"\w+")


def fold(text: str) -> str:
    """Casefold and strip combining marks (đ -> d)."""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _features(text: str) -> list[str]:
    folded = unicodedata.normalize("NFC", text).casefold()
    words = [m.group(0) for m in _WORD.finditer(folded)]
    plain = [m.group(0) for m in _WORD.finditer(fold(text))]
    return [*words, *(f"~{word}" for word in plain)]


@dataclass(slots=True)
class FakeBgeM3Embeddings:
    """``BatchEmbeddingPort`` without a model; counts calls for tests."""

    identity: SearchEmbeddingIdentity = FAKE_BGE_M3
    document_calls: int = 0
    query_calls: int = 0
    texts_embedded: list[str] = field(default_factory=list[str])

    def _vector(self, text: str) -> tuple[float, ...]:
        size = self.identity.dimensions
        values = [0.0] * size
        features = _features(text) or ["<empty>"]
        for feature in features:
            digest = blake2b(feature.encode(), digest_size=8).digest()
            slot = int.from_bytes(digest[:4]) % size
            values[slot] += 1.0 if digest[4] & 1 else -1.0
        if not any(values):
            values[0] = 1.0
        return l2_normalize(tuple(values))

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """One vector per text."""
        self.document_calls += 1
        self.texts_embedded.extend(texts)
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Query vector (same space, no instruction prefix)."""
        self.query_calls += 1
        return self._vector(text)
