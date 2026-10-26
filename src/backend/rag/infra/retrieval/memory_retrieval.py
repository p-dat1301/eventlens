"""In-memory fixture ``RetrievalStore`` for the default API runtime and tests.

Approximates the PostgreSQL retrieval contract (``lower()`` word terms, not the
SQL text-search configuration) over a small fixture corpus. It is a fixture
adapter: no persistence, no network, not a production index. Tombstoned or
replaced articles disappear from every read, exactly like the SQL store's
current-revision views.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from unicodedata import normalize
from uuid import UUID

from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    MetadataEligibility,
    RetrievalScope,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.chunking import chunk_document
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.retrieval import (
    ChunkRef,
    LexicalChunk,
    LexicalLimits,
    LexicalPage,
    RetrievalStorageError,
    VectorHit,
)
from backend.shared.contracts import ArticleEnrichment
from backend.shared.contracts.taxonomy import TopicTag

_WORD = re.compile(r"\w+")


def load_fixture_articles(directory: Path) -> tuple[ArticleDocument, ...]:
    """Validated fixture articles (``articles/NN.json``) in file-name order."""
    return tuple(
        ArticleDocument.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "articles").glob("*.json"))
    )


@dataclass(slots=True)
class InMemoryRetrievalStore:  # noqa: MUTABLE_OK - fixture corpus
    """Thread-safe fixture store; ``available=False`` simulates an outage."""

    documents: dict[UUID, ArticleDocument] = field(
        default_factory=dict[UUID, ArticleDocument]
    )
    vectors: list[VectorHit] = field(default_factory=list[VectorHit])
    metadata: dict[UUID, ArticleEnrichment] = field(
        default_factory=dict[UUID, ArticleEnrichment]
    )
    active: dict[UUID, int] = field(default_factory=dict[UUID, int])
    available: bool = True
    reads: int = 0
    _lock: Lock = field(default_factory=Lock)

    def add(self, document: ArticleDocument, *, vectors: bool = False) -> None:
        """Make ``document`` the current revision (optionally vector-indexed)."""
        with self._lock:
            self.documents[document.article_id] = document
            self.vectors = [
                hit for hit in self.vectors if hit.article_id != document.article_id
            ]
            _ = self.active.pop(document.article_id, None)
            _ = self.metadata.pop(document.article_id, None)
            if not vectors:
                return
            generation = len(self.active) + 1
            self.active[document.article_id] = generation
            self.vectors.extend(
                VectorHit(
                    article_id=document.article_id,
                    content_hash=document.content_hash,
                    generation=generation,
                    chunk_index=chunk.index,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content=chunk.content,
                    source_published_at=document.source_published_at,
                    distance=0.5,
                )
                for chunk in chunk_document(document)
            )

    def tombstone(self, article_id: UUID) -> None:
        """Remove the article from every current-revision read."""
        with self._lock:
            _ = self.documents.pop(article_id, None)
            _ = self.active.pop(article_id, None)
            _ = self.metadata.pop(article_id, None)
            self.vectors = [hit for hit in self.vectors if hit.article_id != article_id]

    def _enter(self) -> None:
        self.reads += 1
        if not self.available:
            raise RetrievalStorageError

    @staticmethod
    def _in_scope(document: ArticleDocument, scope: RetrievalScope) -> bool:
        if scope.source_domains and document.source_domain not in scope.source_domains:
            return False
        published = document.source_published_at
        if scope.published_after is not None and (
            published is None or published < scope.published_after
        ):
            return False
        if scope.published_before is not None and (
            published is None or published >= scope.published_before
        ):
            return False
        allowed = scope.allowed_article_ids
        return allowed is None or document.article_id in allowed

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[TopicTag, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        """Count scoped articles and list those whose metadata matches."""
        with self._lock:
            self._enter()
            scoped = [d for d in self.documents.values() if self._in_scope(d, scope)]
            valid = [d for d in scoped if d.article_id in self.metadata]
            matching = sorted(
                (
                    d.article_id
                    for d in valid
                    if self._matches(self.metadata[d.article_id], topics, entity_ids)
                ),
                key=str,
            )
        return MetadataEligibility(
            scoped_articles=len(scoped),
            verifiable_articles=len(valid),
            matching_article_ids=tuple(matching[:scan_cap]),
            scan_cap_reached=len(matching) > scan_cap,
        )

    @staticmethod
    def _matches(
        metadata: ArticleEnrichment,
        topics: tuple[TopicTag, ...],
        entity_ids: tuple[UUID, ...],
    ) -> bool:
        if topics and not set(topics) & set(metadata.topics):
            return False
        return not entity_ids or any(
            entity.entity_id in entity_ids for entity in metadata.entities
        )

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Bounded word-overlap chunk pool of current documents."""
        words = [m.group(0) for m in _WORD.finditer(normalize("NFC", query).lower())]
        terms = [t for t in dict.fromkeys(words) if t not in limits.stopwords][
            : limits.max_terms
        ]
        with self._lock:
            self._enter()
            documents = [d for d in self.documents.values() if self._in_scope(d, scope)]
        if not terms:
            return LexicalPage(term_count=0, chunks=(), truncated_articles=0)
        wanted = set(terms)
        rows: list[LexicalChunk] = []
        truncated = 0
        for document in documents[: limits.article_pool]:
            title = set(_WORD.findall(document.title.lower()))
            chunks = chunk_document(document)
            if len(chunks) > limits.chunks_scanned_per_article:
                truncated += 1
            found = [
                LexicalChunk(
                    article_id=document.article_id,
                    content_hash=document.content_hash,
                    source_published_at=document.source_published_at,
                    chunk_index=chunk.index,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    matched_terms=matched,
                    document_rank=0.1,
                )
                for chunk in chunks[: limits.chunks_scanned_per_article]
                if (
                    matched := len(
                        (title | set(_WORD.findall(chunk.content.lower()))) & wanted
                    )
                )
            ]
            if found:  # keep ties at the per-article cut, like the SQL rank()
                cut = sorted((c.matched_terms for c in found), reverse=True)
                floor = cut[min(len(cut), limits.chunks_per_article) - 1]
                rows.extend(c for c in found if c.matched_terms >= floor)
        return LexicalPage(
            term_count=len(terms), chunks=tuple(rows), truncated_articles=truncated
        )

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        """Exact code-point slices of the current revisions."""
        with self._lock:
            self._enter()
            missing = [c for c in chunks if c.article_id not in self.documents]
            if missing:
                raise RetrievalStorageError
            return tuple(
                self.documents[c.article_id].normalized_content[
                    c.start_offset : c.end_offset
                ]
                for c in chunks
            )

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        """Active-generation chunks of current documents (fixed distance)."""
        del embedding, identity
        with self._lock:
            self._enter()
            hits = [
                hit
                for hit in self.vectors
                if hit.article_id in self.documents
                and self._in_scope(self.documents[hit.article_id], scope)
                and self.active.get(hit.article_id) == hit.generation
            ]
        return tuple(hits[:limit])

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        """One batched read of current, untombstoned revisions."""
        with self._lock:
            self._enter()
            return tuple(
                CurrentArticleState(
                    document=self.documents[article_id],
                    active_index_generation=self.active.get(article_id),
                    metadata=self.metadata.get(article_id),
                    metadata_generation=(
                        self.active.get(article_id)
                        if article_id in self.metadata
                        else None
                    ),
                )
                for article_id in article_ids
                if article_id in self.documents
            )
