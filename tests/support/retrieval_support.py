"""Shared T5 retrieval test helpers (builders and fakes; not DB evidence alone)."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unicodedata import normalize
from uuid import UUID, uuid4

import psycopg
from pydantic import TypeAdapter

from backend.rag.application.ports.embeddings import EmbeddingPort
from backend.rag.application.ports.reranker import RerankerPort
from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    MetadataEligibility,
    RetrievalScope,
    RetrievalStore,
)
from backend.rag.application.use_cases.retrieve import (
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from backend.rag.domain.chunking import chunk_document
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    ChunkRef,
    LexicalChunk,
    LexicalLimits,
    LexicalPage,
    QueryControl,
    RetrievalPolicy,
    VectorHit,
)
from backend.rag.domain.staging import StageRequest
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.postgres.publication import PostgresPublication, Publication
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.shared.contracts import ArticleEnrichment
from backend.shared.contracts.taxonomy import TopicTag

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "fixtures/rag"
NOW = datetime(2026, 4, 1, tzinfo=UTC)
POLICY = bundled_retrieval_policy()
CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
_JSON = TypeAdapter(dict[str, object])


@dataclass(slots=True)
class MutableAccess:
    """Fixture access policy whose revocation tests can flip mid-query."""

    revoked: bool = False
    checks: int = 0

    def allows(self, scope: CallerScope) -> bool:
        self.checks += 1
        return not self.revoked and scope.policy_id == "fixture-policy"


@dataclass(slots=True)
class FakeMonotonic:
    """Manually advanced monotonic clock (no sleeping)."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def control(seconds: float = 30.0) -> QueryControl:
    return QueryControl(deadline_seconds=seconds)


def article(  # noqa: PLR0913
    content: str,
    *,
    article_id: UUID | None = None,
    title: str = "Harbor report",
    domain: str = "example.test",
    published: datetime | None = None,
    source_name: str = "Example Desk",
) -> ArticleDocument:
    normalized = normalize("NFC", content)
    digest = normalized_content_hash(normalized)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article_id or uuid4(),
            "source_id": UUID("00000000-0000-4000-8000-000000000012"),
            "title": title,
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": normalized,
            "canonical_url": "https://example.test/retrieval",
            "source_name": source_name,
            "source_domain": domain,
            "source_published_at": published,
            "ingested_at": datetime(2026, 3, 1, tzinfo=UTC),
            "processed_at": datetime(2026, 3, 1, tzinfo=UTC),
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


def fixture_article(number: int) -> ArticleDocument:
    path = FIXTURES / f"articles/{number:02d}.json"
    return ArticleDocument.model_validate_json(path.read_text(encoding="utf-8"))


def fixture_enrichment(number: int) -> ArticleEnrichment:
    path = FIXTURES / f"enrichments/{number:02d}.json"
    return ArticleEnrichment.model_validate_json(path.read_text(encoding="utf-8"))


def metadata_for(
    document: ArticleDocument,
    topics: tuple[TopicTag, ...] = ("transportation",),
    entity_id: UUID | None = None,
    version: str = "v1",
) -> ArticleEnrichment:
    """READY metadata for ``document`` with optional entity (grounded span)."""
    source = _JSON.validate_json((FIXTURES / "enrichments/21.json").read_bytes())
    excerpt_end = min(len(document.normalized_content), 12)
    excerpt = document.normalized_content[:excerpt_end]
    span_id = uuid4()
    entity = {
        "entity_id": None if entity_id is None else str(entity_id),
        "entity_type": "ORG",
        "canonical_name": excerpt.strip() or "Entity",
        "surface_forms": [excerpt.strip() or "Entity"],
        "evidence_span_ids": [str(span_id)],
        "confidence": 1.0,
    }
    source.update(
        article_id=str(document.article_id),
        content_hash=document.content_hash,
        enrichment_version=version,
        model_version=version,
        prompt_version=version,
        topics=[str(topic) for topic in topics],
        entities=[entity] if entity_id is not None else [],
        locations=[],
        evidence_spans=[
            {
                "span_id": str(span_id),
                "article_id": str(document.article_id),
                "content_hash": document.content_hash,
                "start_offset": 0,
                "end_offset": excerpt_end,
                "excerpt": excerpt,
                "span_fingerprint": f"sha256:{sha256(excerpt.encode()).hexdigest()}",
                "offset_basis": "unicode_codepoint",
            }
        ],
    )
    return ArticleEnrichment.model_validate_json(_JSON.dump_json(source))


def briefing(text: str, **overrides: object) -> BriefingQuery:
    payload: dict[str, object] = {
        "schema_version": "briefing_query.v1",
        "query": text,
        "publication_time_basis": "source_published_at",
        "result_limit": 20,
        "context_budget": 12_000,
    }
    payload.update(overrides)
    return BriefingQuery.model_validate(payload)


def service(  # noqa: PLR0913
    store: RetrievalStore,
    *,
    embeddings: EmbeddingPort | None = None,
    reranker: RerankerPort | None = None,
    policy: RetrievalPolicy = POLICY,
    access: MutableAccess | None = None,
    identity: EmbeddingIdentity | None = None,
    use_default_reranker: bool = True,
) -> RetrieveEvidence:
    return RetrieveEvidence(
        store=store,
        embeddings=embeddings if embeddings is not None else FakeEmbeddings(),
        embedding_identity=identity or EmbeddingIdentity(),
        reranker=(
            reranker
            if reranker is not None or not use_default_reranker
            else IdentityReranker()
        ),
        tokens=FixtureTokenCounter(),
        access=access or MutableAccess(),
        policy=policy,
        clock=lambda: NOW,
    )


@dataclass(slots=True)
class MemoryStore:
    """In-memory RetrievalStore fake with call counts and per-call hooks."""

    documents: dict[UUID, ArticleDocument] = field(
        default_factory=dict[UUID, ArticleDocument]
    )
    vectors: list[VectorHit] = field(default_factory=list[VectorHit])
    metadata: dict[UUID, ArticleEnrichment] = field(
        default_factory=dict[UUID, ArticleEnrichment]
    )
    active: dict[UUID, int] = field(default_factory=dict[UUID, int])
    calls: list[str] = field(default_factory=list[str])
    hooks: dict[str, Callable[[], None]] = field(
        default_factory=dict[str, Callable[[], None]]
    )
    failure: Exception | None = None

    def add(self, document: ArticleDocument, *, vectors: bool = False) -> None:
        self.documents[document.article_id] = document
        if vectors:
            generation = len(self.active) + 1
            self.active[document.article_id] = generation
            for chunk in chunk_document(document):
                self.vectors.append(
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
                )

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        hook = self.hooks.get(name)
        if hook is not None:
            hook()

    def _in_scope(self, document: ArticleDocument, scope: RetrievalScope) -> bool:
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
        self._enter("metadata_eligibility")
        scoped = [d for d in self.documents.values() if self._in_scope(d, scope)]
        valid = [d for d in scoped if d.article_id in self.metadata]
        matching = sorted(
            (
                d.article_id
                for d in valid
                if (not topics or set(topics) & set(self.metadata[d.article_id].topics))
                and (
                    not entity_ids
                    or any(
                        e.entity_id in entity_ids
                        for e in self.metadata[d.article_id].entities
                    )
                )
            ),
            key=str,
        )
        return MetadataEligibility(
            scoped_articles=len(scoped),
            verifiable_articles=len(valid),
            matching_article_ids=tuple(matching[:scan_cap]),
            scan_cap_reached=len(matching) > scan_cap,
        )

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        """Approximates the SQL contract (lower(), not casefold) for unit tests."""
        self._enter("lexical_chunks")
        words = [
            m.group(0) for m in re.finditer(r"\w+", normalize("NFC", query).lower())
        ]
        terms = [t for t in dict.fromkeys(words) if t not in limits.stopwords][
            : limits.max_terms
        ]
        if not terms:
            return LexicalPage(term_count=0, chunks=(), truncated_articles=0)
        wanted = set(terms)
        rows: list[LexicalChunk] = []
        truncated = 0
        documents = [d for d in self.documents.values() if self._in_scope(d, scope)]
        for document in documents[: limits.article_pool]:
            title = set(re.findall(r"\w+", document.title.lower()))
            chunks = chunk_document(document)
            if len(chunks) > limits.chunks_scanned_per_article:
                truncated += 1
            found: list[LexicalChunk] = []
            for chunk in chunks[: limits.chunks_scanned_per_article]:
                present = title | set(re.findall(r"\w+", chunk.content.lower()))
                matched = len(present & wanted)
                if matched:
                    found.append(
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
                    )
            if found:  # keep ties at the per-article cut, like the SQL rank()
                cut = sorted(c.matched_terms for c in found)[::-1]
                floor = cut[min(len(cut), limits.chunks_per_article) - 1]
                rows.extend(c for c in found if c.matched_terms >= floor)
        return LexicalPage(
            term_count=len(terms), chunks=tuple(rows), truncated_articles=truncated
        )

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        self._enter("chunk_texts")
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
        del embedding, identity
        self._enter("vector_chunks")
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
        self._enter("current_articles")
        return tuple(
            CurrentArticleState(
                document=self.documents[article_id],
                active_index_generation=self.active.get(article_id),
                metadata=self.metadata.get(article_id),
                metadata_generation=self.active.get(article_id)
                if article_id in self.metadata
                else None,
            )
            for article_id in article_ids
            if article_id in self.documents
        )


# Real PostgreSQL helpers -------------------------------------------------


def stage(
    url: str, document: ArticleDocument, identity: EmbeddingIdentity | None = None
) -> int:
    chunks = chunk_document(document)
    vectors = tuple(FakeEmbeddings().embed(chunk.content) for chunk in chunks)
    return PostgresStaging(url).stage(
        StageRequest(document, identity or EmbeddingIdentity(), chunks, vectors)
    )


def publish_ready(
    url: str, document: ArticleDocument, metadata: ArticleEnrichment | None = None
) -> tuple[UUID, int]:
    """Real T4 path: enqueue -> claim -> stage -> fenced READY publication."""
    metadata = metadata or metadata_for(document)
    store = PostgresRagStore(url)
    job = store.enqueue_job(
        document.article_id,
        document.content_hash,
        metadata.enrichment_version,
        metadata.model_version,
        metadata.prompt_version,
    )
    claim = store.claim_job(job)
    assert claim is not None
    generation = stage(url, document)
    assert PostgresPublication(url).publish(
        Publication(job, claim, generation, metadata)
    )
    return job, generation


def execute(url: str, statement: str, params: tuple[object, ...] = ()) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(statement, params)  # pyright: ignore[reportArgumentType]
