"""Per-question article scope for the eval benchmark.

G1-G3 have no per-article access control: the fixture questions express their
allowed scope as ``allowed_article_ids``. ``ScopedRetrievalStore`` wraps the
full in-memory store and narrows every read to the active question's allowed
set (intersected into ``RetrievalScope.allowed_article_ids`` and filtered on
``current_articles``), so one composed runtime serves every question while
each query only sees its own scope. ``scope_violations`` independently checks
what was retrieved, sent to the model and cited. This is eval wiring, not a
production authorization mechanism.
"""

import json
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    MetadataEligibility,
    RetrievalScope,
)
from backend.rag.application.ports.synthesis import SynthesisRequest
from backend.rag.application.use_cases.briefing import BriefingOutcome
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.retrieval import (
    ChunkRef,
    LexicalLimits,
    LexicalPage,
    RetrievalStorageError,
    VectorHit,
)
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from backend.shared.contracts.taxonomy import TopicTag

from .metrics import Json, strs


@dataclass(slots=True)
class ScopedRetrievalStore:  # noqa: MUTABLE_OK - active scope switches per question
    """``RetrievalStore``/``CurrentArticleReader`` limited to the active scope."""

    inner: InMemoryRetrievalStore
    allowed: frozenset[UUID] | None = None

    def _narrow(self, scope: RetrievalScope) -> RetrievalScope:
        if self.allowed is None:
            return scope
        current = scope.allowed_article_ids
        keep = self.allowed if current is None else self.allowed & set(current)
        return replace(scope, allowed_article_ids=tuple(sorted(keep, key=str)))

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[TopicTag, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        """Inner eligibility over the narrowed scope."""
        return self.inner.metadata_eligibility(
            self._narrow(scope), topics, entity_ids, scan_cap
        )

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Inner lexical pool over the narrowed scope."""
        return self.inner.lexical_chunks(query, self._narrow(scope), limits)

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        """Inner vector hits over the narrowed scope."""
        return self.inner.vector_chunks(embedding, identity, self._narrow(scope), limit)

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        """Refuse any chunk outside the active scope."""
        if self.allowed is not None and any(
            c.article_id not in self.allowed for c in chunks
        ):
            raise RetrievalStorageError
        return self.inner.chunk_texts(chunks)

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        """Current revisions of in-scope articles only."""
        wanted = tuple(
            a for a in article_ids if self.allowed is None or a in self.allowed
        )
        return self.inner.current_articles(wanted)


def sent_articles(requests: list[SynthesisRequest]) -> set[UUID]:
    """Article IDs present in the model payloads."""
    found: set[UUID] = set()
    for request in requests:
        for message in request.serialized.messages:
            if message.role != "user":
                continue
            body = cast("dict[str, object]", json.loads(message.content))
            sources = cast("list[dict[str, object]]", body.get("sources", []))
            found.update(UUID(str(s["article_id"])) for s in sources)
    return found


def scope_violations(
    outcome: BriefingOutcome | None,
    requests: list[SynthesisRequest],
    allowed: frozenset[UUID],
) -> dict[str, Json]:
    """Out-of-scope article IDs per stage (retrieved, sent, cited)."""
    retrieved: set[UUID] = set()
    cited: set[UUID] = set()
    if outcome is not None:
        for article in outcome.retrieval.articles:
            retrieved.add(article.article_id)
            retrieved.update(article.duplicate_article_ids)
        cited = {c.article_id for c in outcome.result.answer.citations}
    stages = {
        "retrieved": retrieved,
        "sent_to_model": sent_articles(requests),
        "cited": cited,
    }
    return {
        name: strs(sorted(str(a) for a in ids - allowed))
        for name, ids in stages.items()
    }
