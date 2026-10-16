"""Composition root for RAG ingest and enrichment (adapters injected here only)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.fake_enricher import FakeArticleEnricher
from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.adapters.postgres_enrichment import PostgresEnrichmentRepository
from eventlens_rag.adapters.staging import PostgresStaging
from eventlens_rag.application.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from eventlens_rag.application.index import EnsureGeneration
from eventlens_rag.application.ingest import IngestArticle
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.enrichment_draft import EnrichmentDraft
from eventlens_rag.domain.enrichment_lifecycle import (
    EnrichmentIdentity,
    FailureCode,
    RetryPolicy,
)
from eventlens_rag.graph.enrichment import EnrichmentRunner
from eventlens_rag.ports.enrichment import (
    ArticleEnricher,
    EnrichmentProviderError,
    EnrichmentRequest,
    TelemetrySink,
)

FIXTURE_CALLER: Final = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)


class Provider(StrEnum):
    """Local providers only; no live provider can be selected from the CLI."""

    FAKE = "fake"
    FAKE_TIMEOUT = "fake-timeout"


@dataclass(frozen=True, slots=True)
class FakeTimeoutEnricher:
    """Fault injection: same identity as the fake, every call times out."""

    model_version: str = FakeArticleEnricher().model_version
    prompt_version: str = FakeArticleEnricher().prompt_version

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Simulate a provider deadline expiry without sleeping."""
        del request
        raise EnrichmentProviderError(FailureCode.PROVIDER_TIMEOUT)


def utc_now() -> datetime:
    """Application clock for ingest and enrichment timestamps."""
    return datetime.now(UTC)


def _enricher(provider: Provider) -> ArticleEnricher:
    match provider:
        case Provider.FAKE:
            return FakeArticleEnricher()
        case Provider.FAKE_TIMEOUT:
            return FakeTimeoutEnricher()


@dataclass(frozen=True, slots=True)
class EnrichmentRuntime:
    """Wired services for one CLI invocation."""

    ingest: IngestArticle
    steps: EnrichmentSteps
    policy: RetryPolicy

    def runner(self, telemetry: TelemetrySink) -> EnrichmentRunner:
        """Compile the enrichment graph with the injected telemetry sink."""
        return EnrichmentRunner(self.steps, telemetry)


def build_runtime(
    database_url: str, provider: Provider = Provider.FAKE
) -> EnrichmentRuntime:
    """Wire PostgreSQL storage, fake embeddings and a local enricher."""
    repository = PostgresEnrichmentRepository(database_url)
    enricher = _enricher(provider)
    policy = bundled_retry_policy()
    identity = EnrichmentIdentity(
        enrichment_version=ENRICHMENT_VERSION,
        model_version=enricher.model_version,
        prompt_version=enricher.prompt_version,
    )
    generations = EnsureGeneration(
        PostgresStaging(database_url), FakeEmbeddings(), EmbeddingIdentity()
    )
    steps = EnrichmentSteps(repository, enricher, generations, policy, utc_now)
    ingest = IngestArticle(repository, FixtureAccessPolicy(), identity, utc_now)
    return EnrichmentRuntime(ingest=ingest, steps=steps, policy=policy)
