"""In-memory reference BM25 (Okapi, Lucene IDF) for contract tests and CI.

Same contract as the Elasticsearch adapter: whitespace/word tokenization with
lowercasing (comparable to ``vi-standard.v1``), scope filter before ranking,
deterministic ties (score desc, article_id asc, chunk_index asc).
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.domain.search_index import (
    IndexedChunk,
    LexicalIndexIdentity,
    ScoredChunk,
)

_TOKEN = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens (Unicode word-character runs)."""
    return _TOKEN.findall(text.lower())


def in_scope(item: IndexedChunk, scope: RetrievalScope) -> bool:
    """Source/time/article filters with the PostgreSQL NULL rule."""
    published = item.source_published_at
    if scope.source_domains and item.source_domain not in scope.source_domains:
        return False
    if (scope.published_after or scope.published_before) and published is None:
        return False
    if (
        scope.published_after is not None
        and published is not None
        and published < scope.published_after
    ):
        return False
    if (
        scope.published_before is not None
        and published is not None
        and published >= scope.published_before
    ):
        return False
    return not (
        scope.allowed_article_ids is not None
        and item.chunk.article_id not in scope.allowed_article_ids
    )


@dataclass(slots=True)
class MemoryBm25Index:
    """``ChunkLexicalIndex`` reference implementation."""

    k1: float = 1.2
    b: float = 0.75
    _chunks: list[IndexedChunk] = field(default_factory=list)
    _terms: list[Counter[str]] = field(default_factory=list)
    _df: Counter[str] = field(default_factory=Counter)

    @property
    def identity(self) -> LexicalIndexIdentity:
        """Reference identity (never used for production numbers)."""
        return LexicalIndexIdentity(
            engine="memory-bm25",
            engine_version="1",
            analyzer_id="unicode-word-lower.v1",
            analyzer_digest="none",
            similarity=f"BM25(k1={self.k1},b={self.b})",
        )

    def rebuild(self, chunks: Sequence[IndexedChunk]) -> None:
        """Replace content."""
        self._chunks = list(chunks)
        self._terms = [Counter(tokenize(item.text)) for item in self._chunks]
        self._df = Counter()
        for terms in self._terms:
            self._df.update(terms.keys())

    def search(
        self, query: str, scope: RetrievalScope, limit: int
    ) -> tuple[ScoredChunk, ...]:
        """Lucene-style BM25 over the whole index, filtered by scope."""
        count = len(self._chunks)
        if count == 0:
            return ()
        average = sum(sum(t.values()) for t in self._terms) / count
        query_terms = tokenize(query)
        scored: list[ScoredChunk] = []
        for item, terms in zip(self._chunks, self._terms, strict=True):
            if not in_scope(item, scope):
                continue
            length = sum(terms.values())
            score = 0.0
            for term in query_terms:
                frequency = terms.get(term, 0)
                if frequency == 0:
                    continue
                df = self._df[term]
                idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                norm = self.k1 * (1 - self.b + self.b * length / average)
                score += idf * frequency * (self.k1 + 1) / (frequency + norm)
            if score > 0:
                scored.append(ScoredChunk(item.chunk, score, item.source_published_at))
        scored.sort(
            key=lambda hit: (
                -hit.score,
                str(hit.chunk.article_id),
                hit.chunk.chunk_index,
            )
        )
        return tuple(scored[:limit])

    def size_bytes(self) -> int:
        """Not meaningful in memory."""
        return 0
