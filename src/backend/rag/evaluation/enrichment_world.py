"""In-memory enrichment world driven through the compiled ingestion graph.

The repository/staging are the in-memory adapters (never DB evidence); the
graph is the real ``EnrichmentRunner`` StateGraph with an injected clock. A
metering wrapper counts fixture-tokenizer tokens and wall-clock time per
provider call without changing what the enricher returns.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from backend.rag.application.ports.enrichment import ArticleEnricher, EnrichmentRequest
from backend.rag.application.use_cases.enrichment import (
    CancellationScope,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_draft import EnrichmentDraft
from backend.rag.domain.enrichment_lifecycle import JobOutcome
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.repositories.memory_enrichment import (
    InMemoryEnrichmentRepository,
    InMemoryStaging,
)
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.shared.contracts import ArticleEnrichment

from .corpus import EVAL_CLOCK


@dataclass(slots=True)
class EvalClock:  # noqa: MUTABLE_OK - advanced explicitly, never sleeps
    """Injected application clock."""

    now: datetime = EVAL_CLOCK

    def __call__(self) -> datetime:
        """Current fixed time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward deterministically."""
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """Accounting for one provider call (no article text)."""

    article_id: UUID
    input_tokens: int
    output_tokens: int | None
    latency_ms: float
    failed: bool


@dataclass(slots=True)
class MeteredEnricher:  # noqa: MUTABLE_OK - records calls
    """Pass-through ``ArticleEnricher`` that records tokens and latency."""

    inner: ArticleEnricher
    monotonic: Callable[[], float] = time.perf_counter
    counter: FixtureTokenCounter = field(default_factory=FixtureTokenCounter)
    calls: list[ProviderCall] = field(default_factory=list[ProviderCall])

    @property
    def model_version(self) -> str:
        """Inner enricher model version."""
        return self.inner.model_version

    @property
    def prompt_version(self) -> str:
        """Inner enricher prompt version."""
        return self.inner.prompt_version

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Delegate once; record input/output fixture tokens and wall time."""
        started = self.monotonic()
        tokens_in = self.counter.count(request.context.model_dump_json())
        try:
            draft = self.inner.enrich(request)
        except Exception:
            self._record(request, tokens_in, None, started)
            raise
        self._record(
            request, tokens_in, self.counter.count(draft.model_dump_json()), started
        )
        return draft

    def _record(
        self,
        request: EnrichmentRequest,
        tokens_in: int,
        tokens_out: int | None,
        started: float,
    ) -> None:
        self.calls.append(
            ProviderCall(
                article_id=request.context.article_id,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=(self.monotonic() - started) * 1000,
                failed=tokens_out is None,
            )
        )


@dataclass(slots=True)
class EnrichmentWorld:  # noqa: MUTABLE_OK - one eval scenario
    """Memory repository + staging + clock; graphs compiled per enricher."""

    clock: EvalClock = field(default_factory=EvalClock)
    staging: InMemoryStaging = field(default_factory=InMemoryStaging)
    repository: InMemoryEnrichmentRepository = field(init=False)
    telemetry: RecordingTelemetry = field(default_factory=RecordingTelemetry)

    def __post_init__(self) -> None:
        """Bind the repository to the shared clock."""
        self.repository = InMemoryEnrichmentRepository(clock=self.clock)

    def persist(self, *documents: ArticleDocument) -> None:
        """Persist current revisions without jobs (``enqueue_missing`` adds them)."""
        for document in documents:
            self.repository.persist(document)

    def runner(self, enricher: ArticleEnricher) -> EnrichmentRunner:
        """Compile the real ingestion StateGraph over this world."""
        steps = EnrichmentSteps(
            repository=self.repository,
            enricher=enricher,
            generations=EnsureGeneration(
                self.staging, FakeEmbeddings(), EmbeddingIdentity()
            ),
            policy=bundled_retry_policy(),
            clock=self.clock,
        )
        return EnrichmentRunner(steps, self.telemetry)

    def drain(
        self, enricher: ArticleEnricher, scope: CancellationScope | None = None
    ) -> list[JobOutcome]:
        """Run bounded passes until no job is claimable at the current time."""
        runner = self.runner(enricher)
        limit = bundled_retry_policy().max_jobs_per_pass
        outcomes: list[JobOutcome] = []
        while True:
            report = runner.run(limit, scope)
            outcomes.extend(report.outcomes)
            if not report.outcomes or report.cancelled:
                return outcomes

    def published(self) -> dict[UUID, ArticleEnrichment]:
        """Latest READY metadata per article (publication order)."""
        return {meta.article_id: meta for *_, meta in self.repository.publications}
