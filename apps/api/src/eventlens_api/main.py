"""API composition root and process entry point.

The default runtime is fixture-only: an in-memory fixture corpus, fake
embeddings, identity reranker and the deterministic fake synthesizer. The
activation decision is always evaluated in fixture mode with an empty
environment, so an API key in the process environment never enables a live
provider. ``EVENTLENS_API_RAG_DATABASE_URL`` opts retrieval and source
references into PostgreSQL. The briefing graph is compiled once per runtime,
not per request.
"""

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from eventlens_api.briefing import (
    BriefingServices,
    BriefingTelemetry,
    BriefingUseCase,
    NullBriefingTelemetry,
)
from eventlens_api.briefing_http import (
    CallerResolver,
    FixedCallerResolver,
    build_briefing_router,
    install_error_handlers,
)
from eventlens_api.http import router
from eventlens_api.sources import InspectSource
from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.fake_synthesis import DeterministicFakeSynthesizer
from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.adapters.identity_reranker import IdentityReranker
from eventlens_rag.adapters.memory_retrieval import (
    InMemoryRetrievalStore,
    load_fixture_articles,
)
from eventlens_rag.adapters.memory_source_references import InMemorySourceReferences
from eventlens_rag.adapters.postgres_retrieval import PostgresRetrievalStore
from eventlens_rag.adapters.postgres_source_references import PostgresSourceReferences
from eventlens_rag.adapters.trust import NoTrustedIssuers
from eventlens_rag.application.activation import (
    evaluate_activation,
    read_provider_environment,
)
from eventlens_rag.application.concurrency import ProcessConcurrencyLimiter
from eventlens_rag.application.retrieve import (
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from eventlens_rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_activation_policy_sha256,
    bundled_synthesis_policy,
)
from eventlens_rag.application.synthesize import SynthesizeAnswer
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.activation import ActivationDecision
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.synthesis import ChatModelIdentity
from eventlens_rag.graph.briefing import BriefingGraph
from eventlens_rag.ports.access import AccessPolicy
from eventlens_rag.ports.reranker import RerankerPort
from eventlens_rag.ports.retrieval import RetrievalStore
from eventlens_rag.ports.source_references import SourceReferenceRegistry
from eventlens_rag.ports.synthesis import ConcurrencyLimiter, SynthesisModelPort
from eventlens_rag.ports.tokens import TokenCounter

FIXTURES = Path(__file__).parents[4] / "fixtures/rag"
DATABASE_URL_ENV = "EVENTLENS_API_RAG_DATABASE_URL"
FIXTURE_CALLER = CallerScope(
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


def create_app(runtime: BriefingRuntime | None = None) -> FastAPI:
    """Compose HTTP adapters into application."""
    runtime = runtime or runtime_from_environment(os.environ)
    application = FastAPI(title="EventLens API")
    application.include_router(router)
    application.include_router(
        build_briefing_router(runtime.use_case, runtime.sources, runtime.callers)
    )
    install_error_handlers(application)
    return application


app = create_app()


def run() -> None:
    """Run API server."""
    uvicorn.run("eventlens_api.main:app", host="127.0.0.1", port=8000)
