"""Shared RAG eval plumbing: runtime wiring, scripted drafts and answer checks.

Every runtime is ``compose_briefing`` (real compiled briefing StateGraph) over
an in-memory fixture store with the fixed eval clock. Checks recompute citation
linkage from the store's current documents independently of the T6 validator.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from backend.rag.application.ports.retrieval import RetrievalStore
from backend.rag.application.ports.synthesis import (
    SynthesisModelPort,
    SynthesisRequest,
    SynthesisResponse,
)
from backend.rag.application.ports.telemetry import BriefingTelemetryEvent
from backend.rag.application.use_cases.briefing import BriefingOutcome
from backend.rag.composition import (
    BriefingRuntime,
    compose_briefing,
    fixture_model_identity,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import QueryControl, RetrievalError
from backend.rag.domain.synthesis import ChatModelIdentity, UsageStatus
from backend.rag.domain.synthesis_draft import DRAFT_SCHEMA_VERSION
from backend.rag.domain.synthesis_errors import SynthesisError
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from backend.shared.contracts import ArticleEnrichment

from .corpus import EVAL_CLOCK, EvalCorpus
from .enrichment_world import EnrichmentWorld
from .metrics import Json

Q1 = "When did East Pier close and reopen?"
HARBOR = UUID("00000000-0000-4000-8000-000000000001")
CONTEXT_BUDGET = 12_000
RESULT_LIMIT = 20


def briefing_query(text: str, **overrides: object) -> BriefingQuery:
    """Same query shape and budget for every eval run."""
    payload: dict[str, object] = {
        "schema_version": "briefing_query.v1",
        "query": text,
        "publication_time_basis": "source_published_at",
        "result_limit": RESULT_LIMIT,
        "context_budget": CONTEXT_BUDGET,
    }
    payload.update(overrides)
    return BriefingQuery.model_validate(payload)


@dataclass(slots=True)
class RecordingTelemetry:  # noqa: MUTABLE_OK - collects events
    """``BriefingTelemetry`` that keeps every event."""

    events: list[BriefingTelemetryEvent] = field(
        default_factory=list[BriefingTelemetryEvent]
    )

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Keep one event."""
        self.events.append(event)


@dataclass(slots=True)
class CountingModel:  # noqa: MUTABLE_OK - counts and keeps requests
    """Pass-through ``SynthesisModelPort`` recording every request."""

    inner: SynthesisModelPort
    requests: list[SynthesisRequest] = field(default_factory=list[SynthesisRequest])

    @property
    def identity(self) -> ChatModelIdentity:
        """Inner identity."""
        return self.inner.identity

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Record, then delegate exactly once."""
        self.requests.append(request)
        return self.inner.synthesize(request)


def fake_model() -> CountingModel:
    """The default deterministic fake synthesizer, counted."""
    return CountingModel(DeterministicFakeSynthesizer(fixture_model_identity()))


def runtime(
    store: RetrievalStore,
    model: SynthesisModelPort,
    *,
    revoked: bool = False,
) -> tuple[BriefingRuntime, RecordingTelemetry]:
    """Compose the production briefing graph over ``store`` with a fixed clock."""
    telemetry = RecordingTelemetry()
    composed = compose_briefing(
        store,
        model=model,
        access=FixtureAccessPolicy(revoked=revoked),
        telemetry=telemetry,
        clock=lambda: EVAL_CLOCK,
    )
    return composed, telemetry


def reindex(
    store: InMemoryRetrievalStore,
    documents: dict[UUID, ArticleDocument],
    published: dict[UUID, ArticleEnrichment],
) -> int:
    """Publish READY metadata + fake vectors for current revisions only."""
    count = 0
    for article_id, metadata in sorted(published.items(), key=lambda i: str(i[0])):
        document = documents.get(article_id)
        if document is None or document.content_hash != metadata.content_hash:
            continue
        store.add(document, vectors=True)
        store.metadata[article_id] = metadata
        count += 1
    return count


def hybrid_store(
    corpus: EvalCorpus, only: tuple[UUID, ...] | None = None
) -> InMemoryRetrievalStore:
    """Persisted, fake-enriched and reindexed store (optionally a subset)."""
    articles = [a for a in corpus.articles if only is None or a.article_id in only]
    store = InMemoryRetrievalStore()
    for document in articles:
        store.add(document)
    world = EnrichmentWorld()
    world.persist(*articles)
    _ = world.drain(FakeArticleEnricher())
    _ = reindex(store, dict(store.documents), world.published())
    return store


def user_payload(request: SynthesisRequest) -> dict[str, object]:
    """Parsed user message the model was sent."""
    user = next(m for m in request.serialized.messages if m.role == "user")
    return cast("dict[str, object]", json.loads(user.content))


def first_span(request: SynthesisRequest, article_id: UUID) -> str | None:
    """First sent span ID of one article, if it was sent."""
    sources = cast("list[dict[str, object]]", user_payload(request).get("sources", []))
    for source in sources:
        if source.get("article_id") == str(article_id):
            spans = cast("list[dict[str, str]]", source.get("spans", []))
            return spans[0]["span_id"] if spans else None
    return None


def draft_response(claims: list[tuple[str, str]]) -> SynthesisResponse:
    """Strict draft JSON with (claim text, span id) pairs."""
    draft = {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "evidence_status": "sufficient" if claims else "insufficient",
        "claims": [
            {
                "claim_ref": f"c{index}",
                "text": text,
                "evidence_span_ids": [span_id],
                "temporal_claim": None,
            }
            for index, (text, span_id) in enumerate(claims, start=1)
        ],
        "uncertainties": []
        if claims
        else [{"reason_code": "insufficient_evidence", "claim_ref": None}],
    }
    return SynthesisResponse(
        content=json.dumps(draft),
        usage=None,
        usage_status=UsageStatus.MISSING,
        reported_model=fixture_model_identity().model_id,
    )


def _collapse(text: str) -> str:
    return " ".join(text.split()).casefold()


def answer_checks(
    outcome: BriefingOutcome, current: dict[UUID, ArticleDocument]
) -> dict[str, int]:
    """Independent structural citation checks plus verbatim claim containment."""
    answer = outcome.result.answer
    sent = {d.article_id: d for d in answer.source_documents}
    claims = {c.claim_id: c for c in answer.claims}
    valid = 0
    excerpts: dict[UUID, list[str]] = {}
    for citation in answer.citations:
        document = current.get(citation.article_id)
        source = sent.get(citation.article_id)
        span = (
            None
            if source is None
            else next(
                (
                    s
                    for s in source.evidence_spans
                    if s.span_id == citation.evidence_span_id
                ),
                None,
            )
        )
        if (
            span is not None
            and document is not None
            and citation.claim_id in claims
            and span.content_hash == document.content_hash
            and document.normalized_content[span.start_offset : span.end_offset]
            == span.excerpt
        ):
            valid += 1
            excerpts.setdefault(citation.claim_id, []).append(_collapse(span.excerpt))
    verbatim = sum(
        1
        for claim_id, claim in claims.items()
        if any(_collapse(claim.text) in text for text in excerpts.get(claim_id, []))
    )
    return {
        "citations": len(answer.citations),
        "citations_structurally_valid": valid,
        "claims": len(claims),
        "claims_verbatim_in_cited_span": verbatim,
    }


@dataclass(frozen=True, slots=True)
class Asked:
    """One execution: outcome or typed error code, plus wall time."""

    outcome: BriefingOutcome | None
    error_code: str | None
    wall_ms: float
    reason: str | None = None


def ask(
    composed: BriefingRuntime,
    query: BriefingQuery,
    control: QueryControl | None = None,
) -> Asked:
    """Run one briefing through the compiled graph; typed errors are captured."""
    control = control or composed.use_case.new_control()
    started = time.perf_counter()
    try:
        outcome = composed.use_case.execute(query, control=control)
    except (SynthesisError, RetrievalError) as error:
        reason: object = getattr(error, "reason", None)
        return Asked(
            None,
            str(error.code),
            (time.perf_counter() - started) * 1000,
            None if reason is None else str(reason),
        )
    return Asked(outcome, None, (time.perf_counter() - started) * 1000)


def scripted_model(
    *items: SynthesisResponse | Callable[[SynthesisRequest], SynthesisResponse],
) -> CountingModel:
    """Counted scripted synthesizer (exhausted script = provider unavailable)."""
    return CountingModel(ScriptedSynthesizer(fixture_model_identity(), list(items)))


def failure_summary(
    store: InMemoryRetrievalStore, model: CountingModel, *, revoked: bool = False
) -> dict[str, Json]:
    """Ask Q1 once; report error code, model calls and storage reads."""
    composed, _ = runtime(store, model, revoked=revoked)
    reads = store.reads
    asked = ask(composed, briefing_query(Q1))
    return {
        "error_code": asked.error_code,
        "answer_returned": asked.outcome is not None,
        "model_calls": len(model.requests),
        "store_reads": store.reads - reads,
    }


def ids(values: object) -> list[Json]:
    """String IDs for JSON output."""
    return [str(v) for v in cast("list[object]", values)]
