"""Shared T4 test helpers (fakes and builders; never DB evidence by themselves)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.memory_enrichment import (
    InMemoryEnrichmentRepository,
    InMemoryStaging,
)
from eventlens_rag.application.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from eventlens_rag.application.index import EnsureGeneration
from eventlens_rag.domain.article_document import (
    ArticleDocument,
    normalized_content_hash,
)
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.enrichment_draft import (
    DraftEntity,
    DraftEvidence,
    DraftFieldConfidence,
    EnrichmentDraft,
)
from eventlens_rag.domain.enrichment_lifecycle import EnrichmentIdentity, RetryPolicy
from eventlens_rag.ports.embeddings import EmbeddingVector
from eventlens_rag.ports.enrichment import (
    ArticleEnricher,
    EnrichmentRepository,
)
from eventlens_rag.ports.staging import GenerationStaging

START = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000012")


@dataclass(slots=True)
class ManualClock:
    """Injected clock: tests advance time explicitly, never sleep."""

    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(slots=True)
class CountingEmbeddings:
    """Wrap the T3 fake embedding adapter and count calls."""

    inner: FakeEmbeddings = field(default_factory=FakeEmbeddings)
    calls: int = 0

    def embed(self, text: str) -> EmbeddingVector:
        self.calls += 1
        return self.inner.embed(text)


def document(
    content: str,
    article_id: UUID | None = None,
    published: datetime | None = None,
) -> ArticleDocument:
    digest = normalized_content_hash(content)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article_id or uuid4(),
            "source_id": SOURCE_ID,
            "title": "Harbor report",
            "raw_content": content,
            "normalization_version": "nfc.v1",
            "normalized_content": content,
            "canonical_url": "https://example.test/internal/path?token=secret",
            "source_name": "Example Wire",
            "source_domain": "example.test",
            "source_published_at": published,
            "ingested_at": START,
            "processed_at": START,
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


def evidence(content: str, excerpt: str) -> DraftEvidence:
    start = content.index(excerpt)
    return DraftEvidence(
        start_offset=start, end_offset=start + len(excerpt), excerpt=excerpt
    )


def grounded_draft(content: str, entity: str = "Harbor Authority") -> EnrichmentDraft:
    """Valid draft citing one exact entity span."""
    return EnrichmentDraft(
        topics=("transportation",),
        content_type="news_report",
        event_relevance="actionable_event",
        entities=(
            DraftEntity(
                entity_type="ORG",
                canonical_name=entity,
                surface_forms=(entity,),
                evidence_refs=(0,),
                confidence=0.9,
            ),
        ),
        locations=(),
        suggested_tags=(" Harbor-Closure ", "harbor-closure"),
        evidence=(evidence(content, entity),),
        field_confidence=(DraftFieldConfidence(field="topics", confidence=0.8),),
    )


def identity_of(enricher: ArticleEnricher) -> EnrichmentIdentity:
    return EnrichmentIdentity(
        enrichment_version=ENRICHMENT_VERSION,
        model_version=enricher.model_version,
        prompt_version=enricher.prompt_version,
    )


def steps(  # noqa: PLR0913
    repository: EnrichmentRepository,
    enricher: ArticleEnricher,
    staging: GenerationStaging,
    *,
    embeddings: CountingEmbeddings,
    clock: ManualClock,
    policy: RetryPolicy | None = None,
) -> EnrichmentSteps:
    return EnrichmentSteps(
        repository=repository,
        enricher=enricher,
        generations=EnsureGeneration(staging, embeddings, EmbeddingIdentity()),
        policy=policy or bundled_retry_policy(),
        clock=clock,
    )


@dataclass(slots=True)
class MemoryWorld:
    """In-memory repository, staging, embeddings and clock for unit tests."""

    clock: ManualClock = field(default_factory=ManualClock)
    staging: InMemoryStaging = field(default_factory=InMemoryStaging)
    embeddings: CountingEmbeddings = field(default_factory=CountingEmbeddings)
    repository: InMemoryEnrichmentRepository = field(init=False)

    def __post_init__(self) -> None:
        self.repository = InMemoryEnrichmentRepository(clock=self.clock)

    def steps(
        self, enricher: ArticleEnricher, policy: RetryPolicy | None = None
    ) -> EnrichmentSteps:
        return steps(
            self.repository,
            enricher,
            self.staging,
            embeddings=self.embeddings,
            clock=self.clock,
            policy=policy,
        )
