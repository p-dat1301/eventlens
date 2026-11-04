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

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.candidates import CandidateSource
from backend.rag.application.ports.embeddings import BatchEmbeddingPort
from backend.rag.application.ports.enrichment import ArticleEnricher, TelemetrySink
from backend.rag.application.ports.reranker import RerankerPort
from backend.rag.application.ports.retrieval import RetrievalStore
from backend.rag.application.ports.search_index import SearchIndexPort
from backend.rag.application.ports.source_references import SourceReferenceRegistry
from backend.rag.application.ports.summary import ArticleSummaryModelPort
from backend.rag.application.ports.synthesis import (
    ConcurrencyLimiter,
    SynthesisModelPort,
)
from backend.rag.application.ports.telemetry import BriefingTelemetry
from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.application.retrieval_profiles import bundled_tuning
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
from backend.rag.application.use_cases.catalog import CatalogQueries
from backend.rag.application.use_cases.conversations import Conversations, Sessions
from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.application.use_cases.inspect_source import InspectSource
from backend.rag.application.use_cases.labels import SyncArticleLabels
from backend.rag.application.use_cases.query_runs import QueryRunExecutor
from backend.rag.application.use_cases.retrieve import (
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.search_candidates import (
    IndexedCandidateSource,
)
from backend.rag.application.use_cases.search_projection import ProjectRevision
from backend.rag.application.use_cases.search_sync import (
    DrainOutbox,
    RebuildSearchIndex,
    ReconcileSearchIndex,
    SearchIndexHealthCheck,
)
from backend.rag.application.use_cases.summarize import SummarySteps
from backend.rag.application.use_cases.synthesize import SynthesizeAnswer
from backend.rag.domain.access import CallerScope
from backend.rag.domain.activation import ActivationDecision
from backend.rag.domain.catalog import CursorCodec
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity, RetryPolicy
from backend.rag.domain.query_intent import DEFAULT_HOP_POLICY, HopPolicy
from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    BASELINE_TUNING_ID,
    InvalidTuningError,
    RetrievalTuning,
)
from backend.rag.domain.synthesis import ChatModelIdentity
from backend.rag.domain.synthesis_errors import ProviderActivationError
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.access.trust import NoTrustedIssuers
from backend.rag.infra.elasticsearch.client import ElasticsearchClient
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.bge_m3 import BgeM3Embeddings
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher, FakeTimeoutEnricher
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.llm.http_transport import UrllibJsonTransport
from backend.rag.infra.llm.summarizers import (
    FixtureSummarizer,
    OpenAICompatibleSummarizer,
)
from backend.rag.infra.postgres.alembic_runner import upgrade_head
from backend.rag.infra.postgres.article_catalog import (
    PostgresArticleCatalog,
    PostgresArticleLabels,
)
from backend.rag.infra.postgres.conversations import (
    PostgresConversations,
    PostgresSessions,
)
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.search_platform import (
    ActiveGenerationCache,
    PostgresEmbeddingCache,
    PostgresOutboxStore,
    PostgresProjectionSource,
    PostgresSearchGenerations,
)
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.summaries import PostgresSummaries
from backend.rag.infra.postgres.work_queue import PostgresWorkQueue
from backend.rag.infra.repositories.memory_source_references import (
    InMemorySourceReferences,
)
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.rerankers.cross_encoder import CrossEncoderReranker
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.memory_retrieval import (
    InMemoryRetrievalStore,
    load_fixture_articles,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.search.memory_index import InMemorySearchIndex
from backend.rag.infra.telemetry.null import NullBriefingTelemetry
from backend.rag.infra.telemetry.telemetry import JsonLinesTelemetry
from backend.rag.presentation.briefing_http import (
    CallerResolver,
    FixedCallerResolver,
)
from backend.rag.presentation.catalog_http import ServiceAuth
from backend.rag.presentation.conversations_http import ChatServices
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.rag.workflows.query.workflow import BriefingGraph
from backend.rag.workflows.summarization.workflow import SummaryRunner

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
    candidates: CandidateSource | None = None,
    fallback_to_store: bool = True,
    hops: HopPolicy = DEFAULT_HOP_POLICY,
    tuning: RetrievalTuning = BASELINE_TUNING,
) -> BriefingRuntime:
    """Wire T5 + T6 behind the compiled briefing graph (fixture mode only).

    ``candidates`` (a search index) replaces the PostgreSQL branches; the
    PostgreSQL final check always runs. With ``fallback_to_store`` an index
    outage falls back to the PostgreSQL branches instead of failing.
    """
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
            candidates=candidates,
            fallback_to_store=fallback_to_store,
            tuning=tuning,
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
        hops=hops,
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
    ``EVENTLENS_RAG_SEARCH_BACKEND=elasticsearch`` (with a database URL) adds
    the search index as candidate source; see ``SearchSettings``.
    """
    settings = SearchSettings.from_environment(environ)
    url = environ.get(DATABASE_URL_ENV, "").strip()
    hops = HopPolicy(enabled=settings.multi_hop)
    reranker = settings.reranker()
    tuning = settings.retrieval_tuning()
    if not url:
        return compose_briefing(
            fixture_store(), reranker=reranker, hops=hops, tuning=tuning
        )
    candidates = None
    if settings.backend is SearchBackend.ELASTICSEARCH:
        engine = rag_engine(url)
        candidates = IndexedCandidateSource(
            index=settings.index(),
            embedder=settings.embedder(),
            generations=ActiveGenerationCache(PostgresSearchGenerations(engine)),
        )
    return compose_briefing(
        PostgresRetrievalStore(rag_engine(url)),
        references=PostgresSourceReferences(rag_engine(url)),
        reranker=reranker,
        candidates=candidates,
        fallback_to_store=settings.fallback_to_postgres,
        hops=hops,
        tuning=tuning,
    )


# --- search platform (derived Elasticsearch projection) --------------------


class SearchBackend(StrEnum):
    """Where retrieval candidates come from (PostgreSQL stays the truth)."""

    POSTGRES = "postgres"
    ELASTICSEARCH = "elasticsearch"
    MEMORY = "memory"


class SearchEmbeddings(StrEnum):
    """Search-index embedder; real models need explicit activation."""

    NONE = "none"
    FAKE_BGE_M3 = "fake-bge-m3"
    BGE_M3 = "bge-m3"


class RerankMode(StrEnum):
    """``off`` keeps the identity (no-op) reranker: fused order is kept."""

    OFF = "off"
    CROSS_ENCODER = "cross-encoder"


class SearchConfigurationError(ValueError):
    """Invalid or incomplete search settings (names only, never values)."""


def _choice[E: StrEnum](
    environ: Mapping[str, str], name: str, kind: type[E], default: E
) -> E:
    raw = environ.get(name, "").strip().lower() or default.value
    try:
        return kind(raw)
    except ValueError as error:
        msg = f"{name} must be one of {[item.value for item in kind]}"
        raise SearchConfigurationError(msg) from error


@dataclass(frozen=True, slots=True)
class SearchSettings:
    """Search configuration read once from the environment.

    | Variable | Default |
    | --- | --- |
    | ``EVENTLENS_RAG_SEARCH_BACKEND`` | ``postgres`` |
    | ``EVENTLENS_RAG_SEARCH_FALLBACK`` | ``postgres`` (``none`` fails closed) |
    | ``EVENTLENS_RAG_ELASTICSEARCH_URL`` / ``..._API_KEY`` | unset |
    | ``EVENTLENS_RAG_SEARCH_EMBEDDINGS`` | ``none`` |
    | ``EVENTLENS_RAG_BGE_M3_REVISION`` | unset (required for ``bge-m3``) |
    | ``EVENTLENS_RAG_RERANK`` | ``off`` |
    | ``EVENTLENS_RAG_RERANK_REVISION`` | unset (required for cross-encoder) |
    | ``EVENTLENS_RAG_MULTI_HOP`` | ``off`` |
    | ``EVENTLENS_RAG_RETRIEVAL_TUNING`` | ``baseline`` (frozen v3 behaviour) |
    """

    backend: SearchBackend = SearchBackend.POSTGRES
    fallback_to_postgres: bool = True
    elasticsearch_url: str = ""
    elasticsearch_api_key: str | None = None
    embeddings: SearchEmbeddings = SearchEmbeddings.NONE
    bge_m3_revision: str = ""
    rerank: RerankMode = RerankMode.OFF
    rerank_revision: str = ""
    multi_hop: bool = False
    device: str = "cpu"
    tuning_profile: str = BASELINE_TUNING_ID

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "SearchSettings":
        """Parse and validate; unknown values fail fast."""
        fallback = environ.get("EVENTLENS_RAG_SEARCH_FALLBACK", "postgres").strip()
        if fallback not in {"postgres", "none"}:
            msg = "EVENTLENS_RAG_SEARCH_FALLBACK must be postgres or none"
            raise SearchConfigurationError(msg)
        hop = environ.get("EVENTLENS_RAG_MULTI_HOP", "off").strip().lower()
        if hop not in {"on", "off"}:
            msg = "EVENTLENS_RAG_MULTI_HOP must be on or off"
            raise SearchConfigurationError(msg)
        settings = cls(
            backend=_choice(
                environ,
                "EVENTLENS_RAG_SEARCH_BACKEND",
                SearchBackend,
                SearchBackend.POSTGRES,
            ),
            fallback_to_postgres=fallback == "postgres",
            elasticsearch_url=environ.get(
                "EVENTLENS_RAG_ELASTICSEARCH_URL", ""
            ).strip(),
            elasticsearch_api_key=(
                environ.get("EVENTLENS_RAG_ELASTICSEARCH_API_KEY", "").strip() or None
            ),
            embeddings=_choice(
                environ,
                "EVENTLENS_RAG_SEARCH_EMBEDDINGS",
                SearchEmbeddings,
                SearchEmbeddings.NONE,
            ),
            bge_m3_revision=environ.get("EVENTLENS_RAG_BGE_M3_REVISION", "").strip(),
            rerank=_choice(environ, "EVENTLENS_RAG_RERANK", RerankMode, RerankMode.OFF),
            rerank_revision=environ.get("EVENTLENS_RAG_RERANK_REVISION", "").strip(),
            multi_hop=hop == "on",
            device=environ.get("EVENTLENS_RAG_MODEL_DEVICE", "cpu").strip() or "cpu",
            tuning_profile=environ.get(
                "EVENTLENS_RAG_RETRIEVAL_TUNING", BASELINE_TUNING_ID
            ).strip()
            or BASELINE_TUNING_ID,
        )
        _ = settings.retrieval_tuning()  # unknown profile fails at startup
        if settings.backend is SearchBackend.ELASTICSEARCH and not (
            settings.elasticsearch_url
        ):
            msg = "EVENTLENS_RAG_ELASTICSEARCH_URL is required for elasticsearch"
            raise SearchConfigurationError(msg)
        if settings.embeddings is SearchEmbeddings.BGE_M3 and not (
            settings.bge_m3_revision
        ):
            msg = "EVENTLENS_RAG_BGE_M3_REVISION is required to activate bge-m3"
            raise SearchConfigurationError(msg)
        if settings.rerank is RerankMode.CROSS_ENCODER and not settings.rerank_revision:
            msg = "EVENTLENS_RAG_RERANK_REVISION is required for cross-encoder"
            raise SearchConfigurationError(msg)
        return settings

    def retrieval_tuning(self) -> RetrievalTuning:
        """Named tuning profile; ``baseline`` is the frozen v3 behaviour."""
        try:
            return bundled_tuning(self.tuning_profile)
        except InvalidTuningError as error:
            raise SearchConfigurationError(str(error)) from None

    def index(self) -> SearchIndexPort:
        """The configured search index adapter."""
        if self.backend is SearchBackend.MEMORY:
            return InMemorySearchIndex()
        return ElasticsearchSearchIndex(
            ElasticsearchClient(self.elasticsearch_url, self.elasticsearch_api_key)
        )

    def embedder(self) -> BatchEmbeddingPort | None:
        """Search embedder; BGE-M3 only with a pinned revision (no download)."""
        match self.embeddings:
            case SearchEmbeddings.NONE:
                return None
            case SearchEmbeddings.FAKE_BGE_M3:
                return FakeBgeM3Embeddings()
            case SearchEmbeddings.BGE_M3:
                return BgeM3Embeddings(
                    revision=self.bge_m3_revision, device=self.device
                )

    def reranker(self) -> RerankerPort:
        """Identity (no-op) reranker unless the cross-encoder is activated."""
        if self.rerank is RerankMode.CROSS_ENCODER:
            return CrossEncoderReranker(
                revision=self.rerank_revision, device=self.device
            )
        return IdentityReranker()


@dataclass(frozen=True, slots=True)
class SearchRuntime:
    """Maintenance use cases of the derived search projection."""

    index: SearchIndexPort
    embedder: BatchEmbeddingPort | None
    drain: DrainOutbox
    rebuild: RebuildSearchIndex
    reconcile: ReconcileSearchIndex
    health: SearchIndexHealthCheck
    outbox: PostgresOutboxStore


def build_search_runtime(
    database_url: str,
    settings: SearchSettings,
    index: SearchIndexPort | None = None,
) -> SearchRuntime:
    """Wire PostgreSQL truth, the index adapter and the embedder."""
    engine = rag_engine(database_url)
    index = index or settings.index()
    embedder = settings.embedder()
    source = PostgresProjectionSource(engine)
    generations = PostgresSearchGenerations(engine)
    outbox = PostgresOutboxStore(engine)
    projector = ProjectRevision(embedder, PostgresEmbeddingCache(engine))
    return SearchRuntime(
        index=index,
        embedder=embedder,
        drain=DrainOutbox(outbox, source, index, generations, projector),
        rebuild=RebuildSearchIndex(source, index, generations, projector),
        reconcile=ReconcileSearchIndex(source, index, generations, projector),
        health=SearchIndexHealthCheck(index, generations, outbox),
        outbox=outbox,
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
    engine = rag_engine(database_url)
    repository = PostgresEnrichmentRepository(engine)
    enricher = _enricher(provider)
    policy = bundled_retry_policy()
    identity = EnrichmentIdentity(
        enrichment_version=ENRICHMENT_VERSION,
        model_version=enricher.model_version,
        prompt_version=enricher.prompt_version,
    )
    generations = EnsureGeneration(
        PostgresStaging(engine), FakeEmbeddings(), EmbeddingIdentity()
    )
    steps = EnrichmentSteps(repository, enricher, generations, policy, utc_now)
    ingest = IngestArticle(repository, FixtureAccessPolicy(), identity, utc_now)
    return EnrichmentRuntime(ingest=ingest, steps=steps, policy=policy)


def json_lines_telemetry(write: Callable[[str], None]) -> TelemetrySink:
    """Enrichment telemetry sink printing one JSON line per job outcome."""
    return JsonLinesTelemetry(write)


def prepare_storage(database_url: str) -> None:
    """Apply pending Alembic revisions (idempotent); never adopts or stamps.

    A database built by the historical runner fails closed with
    ``LegacyAdoptionRequiredError`` until an operator adopts it explicitly.
    """
    _ = upgrade_head(database_url)


# --- news backend: catalog, summaries, conversations -----------------------

INTERNAL_TOKEN_ENV = "EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN"  # noqa: S105 - name
CURSOR_SECRET_ENV = "EVENTLENS_RAG_CURSOR_SECRET"  # noqa: S105 - variable name
ANSWER_PROVIDER_ENV = "EVENTLENS_RAG_ANSWER_PROVIDER"
SUMMARY_PROVIDER_ENV = "EVENTLENS_RAG_SUMMARY_PROVIDER"
TRUSTED_ENRICHMENT_ENV = "EVENTLENS_RAG_TRUSTED_ENRICHMENT_MODELS"


class NewsConfigurationError(ValueError):
    """Invalid news-backend settings (variable names only, never values)."""


class AnswerProvider(StrEnum):
    """Who writes conversation answers; ``none`` refuses runs (503)."""

    NONE = "none"
    FIXTURE = "fixture"  # explicit demo: deterministic fake synthesizer, mode=fixture
    LIVE = "live"  # live activation; fails closed without an approved artifact


class SummaryProvider(StrEnum):
    """Who writes article summaries; ``none`` keeps jobs pending."""

    NONE = "none"
    FIXTURE = "fixture"  # explicit demo: extractive, identity fixture-summarizer
    OPENAI_COMPATIBLE = "openai-compatible"


def _env_choice[E: StrEnum](
    environ: Mapping[str, str], name: str, kind: type[E], default: E
) -> E:
    raw = environ.get(name, "").strip().lower() or default.value
    try:
        return kind(raw)
    except ValueError as error:
        msg = f"{name} must be one of {[item.value for item in kind]}"
        raise NewsConfigurationError(msg) from error


def _cursor_codec(environ: Mapping[str, str]) -> CursorCodec:
    secret = environ.get(CURSOR_SECRET_ENV, "").encode()
    if secret and len(secret) < 32:  # noqa: PLR2004 - minimum HMAC key length
        msg = f"{CURSOR_SECRET_ENV} must be at least 32 bytes"
        raise NewsConfigurationError(msg)
    return CursorCodec(secret or secrets.token_bytes(32))


@dataclass(frozen=True, slots=True)
class NewsRuntime:
    """Catalog and chat services for rag-api (``None`` without a database)."""

    catalog: CatalogQueries | None
    service_auth: ServiceAuth
    chat: ChatServices


def news_runtime(
    environ: Mapping[str, str], briefing: BriefingRuntime | None = None
) -> NewsRuntime:
    """Wire catalog/chat over ``EVENTLENS_API_RAG_DATABASE_URL`` when set."""
    url = environ.get(DATABASE_URL_ENV, "").strip()
    token = environ.get(INTERNAL_TOKEN_ENV, "").strip() or None
    answers = _env_choice(
        environ, ANSWER_PROVIDER_ENV, AnswerProvider, AnswerProvider.NONE
    )
    secure = environ.get("EVENTLENS_RAG_SESSION_COOKIE_SECURE", "0").strip() == "1"
    auth = ServiceAuth(token)
    if not url:
        return NewsRuntime(None, auth, ChatServices(None, None, secure))
    engine = rag_engine(url)
    content = environ.get("EVENTLENS_RAG_CATALOG_CONTENT", "full").strip() or "full"
    if content not in {"full", "withheld"}:
        msg = "EVENTLENS_RAG_CATALOG_CONTENT must be full or withheld"
        raise NewsConfigurationError(msg)
    store = PostgresArticleCatalog(engine, content)
    references = (
        briefing.sources.references
        if briefing is not None
        else PostgresSourceReferences(engine)
    )
    conversations = Conversations(
        store=PostgresConversations(engine),
        catalog=store,
        sources=PostgresRetrievalStore(engine),
        references=references,
        access=FixtureAccessPolicy(),
        caller=FIXTURE_CALLER,
        answers_enabled=answers is not AnswerProvider.NONE,
    )
    return NewsRuntime(
        catalog=CatalogQueries(store, _cursor_codec(environ)),
        service_auth=auth,
        chat=ChatServices(Sessions(PostgresSessions(engine)), conversations, secure),
    )


def build_label_sync(
    database_url: str, environ: Mapping[str, str]
) -> SyncArticleLabels:
    """Label projection; only explicitly trusted enrichment models count."""
    trusted = frozenset(
        item.strip()
        for item in environ.get(TRUSTED_ENRICHMENT_ENV, "").split(",")
        if item.strip()
    )
    return SyncArticleLabels(PostgresArticleLabels(rag_engine(database_url)), trusted)


def _summary_model(environ: Mapping[str, str]) -> ArticleSummaryModelPort | None:
    match _env_choice(
        environ, SUMMARY_PROVIDER_ENV, SummaryProvider, SummaryProvider.NONE
    ):
        case SummaryProvider.NONE:
            return None
        case SummaryProvider.FIXTURE:
            return FixtureSummarizer()
        case SummaryProvider.OPENAI_COMPATIBLE:
            names = (
                "EVENTLENS_RAG_SUMMARY_BASE_URL",
                "EVENTLENS_RAG_SUMMARY_MODEL",
                "EVENTLENS_RAG_SUMMARY_MODEL_VERSION",
            )
            values = [environ.get(name, "").strip() for name in names]
            missing = [n for n, v in zip(names, values, strict=True) if not v]
            if missing:
                msg = f"openai-compatible summaries need {missing}"
                raise NewsConfigurationError(msg)
            base_url, model, version = values
            return OpenAICompatibleSummarizer(
                transport=UrllibJsonTransport(),
                base_url=base_url,
                model=model,
                model_version=version,
                api_key=environ.get("EVENTLENS_RAG_SUMMARY_API_KEY", "").strip()
                or None,
            )


def summary_provider_configured(environ: Mapping[str, str]) -> bool:
    """Whether a summary provider (fixture or real) is selected."""
    return _summary_model(environ) is not None


def build_summary_runner(
    database_url: str,
    environ: Mapping[str, str],
    model: ArticleSummaryModelPort | None = None,
) -> SummaryRunner:
    """Summarization graph over PostgreSQL; refuses to run without a model."""
    model = model or _summary_model(environ)
    if model is None:
        msg = f"{SUMMARY_PROVIDER_ENV} is not configured; summary jobs stay pending"
        raise NewsConfigurationError(msg)
    engine = rag_engine(database_url)
    return SummaryRunner(
        SummarySteps(PostgresSummaries(engine), model, PostgresWorkQueue(engine))
    )


def build_run_executor(
    database_url: str,
    environ: Mapping[str, str],
    *,
    model: SynthesisModelPort | None = None,
) -> QueryRunExecutor:
    """Conversation run executor over the compiled briefing graph.

    ``none`` and a refused ``live`` activation both compose without a model:
    runs then fail with ``model_unavailable`` instead of using a fake.
    """
    engine = rag_engine(database_url)
    provider = _env_choice(
        environ, ANSWER_PROVIDER_ENV, AnswerProvider, AnswerProvider.NONE
    )
    briefing: BriefingUseCase | None = None
    unavailable = "provider_not_configured"
    if provider is AnswerProvider.FIXTURE or model is not None:
        settings = SearchSettings.from_environment(environ)
        candidates = None
        if settings.backend is SearchBackend.ELASTICSEARCH:
            candidates = IndexedCandidateSource(
                index=settings.index(),
                embedder=settings.embedder(),
                generations=ActiveGenerationCache(PostgresSearchGenerations(engine)),
            )
        briefing = compose_briefing(
            PostgresRetrievalStore(engine),
            model=model,
            references=PostgresSourceReferences(engine),
            reranker=settings.reranker(),
            candidates=candidates,
            fallback_to_store=settings.fallback_to_postgres,
            hops=HopPolicy(enabled=settings.multi_hop),
            tuning=settings.retrieval_tuning(),
        ).use_case
    elif provider is AnswerProvider.LIVE:
        unavailable = live_activation_refusal(environ)
    store = PostgresConversations(engine)
    return QueryRunExecutor(
        store=store,
        queue=PostgresWorkQueue(engine),
        catalog=PostgresArticleCatalog(engine),
        briefing=briefing,
        caller=FIXTURE_CALLER,
        unavailable_reason=unavailable,
    )


def live_activation_refusal(environ: Mapping[str, str]) -> str:
    """Evaluate live activation; T6 ships no trusted issuer so it is refused."""
    counter = FixtureTokenCounter()
    try:
        _ = evaluate_activation(
            bundled_activation_policy(),
            bundled_activation_policy_sha256(),
            bundled_synthesis_policy(),
            read_provider_environment(environ),
            "live",
            NoTrustedIssuers(),
            counter.identity,
        )
    except ProviderActivationError as error:
        return str(getattr(error, "reason", "activation_refused"))
    return "live_synthesis_adapter_not_wired"
