"""RAG composition root: the only place that instantiates adapters.

Two runtimes are wired here, each compiled once at process startup:

* query (rag-api): ``compose_briefing`` / ``runtime_from_environment``;
* ingestion (rag-worker): ``build_runtime`` for ingest and enrichment.

The default query runtime is fixture-only: an in-memory fixture corpus, fake
embeddings, identity reranker and the deterministic fake synthesizer. The
activation decision is always evaluated in fixture mode with an empty
environment, so an API key in the process environment never enables a live
provider. ``EVENTLENS_API_RAG_DATABASE_URL`` opts retrieval and source
references into PostgreSQL. The briefing graph is compiled once per runtime,
not per request.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.enrichment import ArticleEnricher, TelemetrySink
from backend.rag.application.ports.reranker import RerankerPort
from backend.rag.application.ports.retrieval import RetrievalStore
from backend.rag.application.ports.source_references import SourceReferenceRegistry
from backend.rag.application.ports.synthesis import (
    ConcurrencyLimiter,
    SynthesisModelPort,
)
from backend.rag.application.ports.telemetry import BriefingTelemetry
from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_activation_policy_sha256,
    bundled_synthesis_policy,
)
from backend.rag.application.use_cases.activation import (
    evaluate_activation,
    read_provider_environment,
)
from backend.rag.application.use_cases.briefing import (
    BriefingServices,
    BriefingUseCase,
)
from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.application.use_cases.inspect_source import InspectSource
from backend.rag.application.use_cases.retrieve import (
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.synthesize import SynthesizeAnswer
from backend.rag.domain.access import CallerScope
from backend.rag.domain.activation import ActivationDecision
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity, RetryPolicy
from backend.rag.domain.synthesis import ChatModelIdentity
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.access.trust import NoTrustedIssuers
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher, FakeTimeoutEnricher
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.postgres.migrations import migrate
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.repositories.memory_source_references import (
    InMemorySourceReferences,
)
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.memory_retrieval import (
    InMemoryRetrievalStore,
    load_fixture_articles,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.telemetry.null import NullBriefingTelemetry
from backend.rag.infra.telemetry.telemetry import JsonLinesTelemetry
from backend.rag.presentation.briefing_http import (
    CallerResolver,
    FixedCallerResolver,
)
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.rag.workflows.query.workflow import BriefingGraph

FIXTURES = Path(__file__).parents[3] / "fixtures/rag"
DATABASE_URL_ENV = "EVENTLENS_API_RAG_DATABASE_URL"
FIXTURE_CALLER: Final = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)


def fixture_activation(counter: TokenCounter) -> ActivationDecision:
    """Fixture decision; the empty environment means no key is ever read."""
    return evaluate_activation(
        bundled_activation_policy(),
        bundled_activation_policy_sha256(),
        bundled_synthesis_policy(),
        read_provider_environment({}),
        "fixture",
        NoTrustedIssuers(),
        counter.identity,
    )


def fixture_model_identity() -> ChatModelIdentity:
    """Chat model identity bound by the fixture activation artifact."""
    binding = bundled_activation_policy().chat_model
    return ChatModelIdentity(binding.model_id, binding.model_version)


@dataclass(frozen=True, slots=True)
class BriefingRuntime:
    """One composed runtime: use cases plus the compiled graph."""

    use_case: BriefingUseCase
    sources: InspectSource
    graph: BriefingGraph
    callers: CallerResolver


def compose_briefing(  # noqa: PLR0913 - composition root wiring
    store: RetrievalStore,
    *,
    model: SynthesisModelPort | None = None,
    access: AccessPolicy | None = None,
    reranker: RerankerPort | None = None,
    limiter: ConcurrencyLimiter | None = None,
    references: SourceReferenceRegistry | None = None,
    telemetry: BriefingTelemetry | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    caller: CallerScope = FIXTURE_CALLER,
) -> BriefingRuntime:
    """Wire T5 + T6 behind the compiled briefing graph (fixture mode only)."""
    counter = FixtureTokenCounter()  # one counter shared by T5 and T6
    synthesis_policy = bundled_synthesis_policy()
    activation = fixture_activation(counter)
    access = access or FixtureAccessPolicy()
    references = references or InMemorySourceReferences()
    services = BriefingServices(
        retriever=RetrieveEvidence(
            store=store,
            embeddings=FakeEmbeddings(),
            embedding_identity=EmbeddingIdentity(),
            reranker=reranker or IdentityReranker(),
            tokens=counter,
            access=access,
            policy=bundled_retrieval_policy(),
            clock=clock,
        ),
        synthesizer=SynthesizeAnswer(
            model=model or DeterministicFakeSynthesizer(fixture_model_identity()),
            sources=store,
            access=access,
            tokens=counter,
            policy=synthesis_policy,
            activation=activation,
        ),
        access=access,
        tokens=counter,
        policy=synthesis_policy,
    )
    graph = BriefingGraph(services)
    use_case = BriefingUseCase(
        workflow=graph,
        services=services,
        limiter=limiter
        or ProcessConcurrencyLimiter(
            synthesis_policy.concurrency.max_concurrent_requests
        ),
        activation=activation,
        caller=caller,
        references=references,
        telemetry=telemetry or NullBriefingTelemetry(),
        monotonic=monotonic,
    )
    sources = InspectSource(references=references, sources=store, access=access)
    return BriefingRuntime(use_case, sources, graph, FixedCallerResolver(caller))


def fixture_store(fixtures: Path = FIXTURES) -> InMemoryRetrievalStore:
    """Fixture corpus without vectors or READY metadata (lexical-only)."""
    store = InMemoryRetrievalStore()
    for document in load_fixture_articles(fixtures):
        store.add(document)
    return store


def runtime_from_environment(environ: Mapping[str, str]) -> BriefingRuntime:
    """Fixture runtime, or PostgreSQL storage when explicitly opted in.

    ``EVENTLENS_API_RAG_DATABASE_URL`` (an already migrated RAG database)
    switches retrieval and source references to PostgreSQL. The model stays
    the fixture synthesizer either way; no provider variable is read.
    """
    url = environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        return compose_briefing(fixture_store())
    return compose_briefing(
        PostgresRetrievalStore(url), references=PostgresSourceReferences(url)
    )


# --- ingestion runtime (rag-worker) ----------------------------------------


class Provider(StrEnum):
    """Local providers only; no live provider can be selected from the CLI."""

    FAKE = "fake"
    FAKE_TIMEOUT = "fake-timeout"


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


def json_lines_telemetry(write: Callable[[str], None]) -> TelemetrySink:
    """Enrichment telemetry sink printing one JSON line per job outcome."""
    return JsonLinesTelemetry(write)


def prepare_storage(database_url: str) -> None:
    """Apply checksum-verified RAG migrations (idempotent)."""
    migrate(database_url)
