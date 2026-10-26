"""Stop-work, admission and outage scenario cases for ``rag_cases``.

Cancellation, budget and capacity must stop further model/storage work; a
storage, enrichment or model outage must be typed and never fall back to raw
model output. Expectations come from the T5-T7 contract.
"""

from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.application.synthesis_policies import bundled_synthesis_policy
from backend.rag.domain.retrieval import QueryControl
from backend.rag.infra.llm.fake_enricher import FakeTimeoutEnricher
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore

from .corpus import EvalCorpus
from .enrichment_world import EnrichmentWorld
from .metrics import Json, strs
from .rag_support import (
    HARBOR,
    Q1,
    ask,
    briefing_query,
    draft_response,
    failure_summary,
    fake_model,
    first_span,
    hybrid_store,
    reindex,
    runtime,
    scripted_model,
)

type Pair = tuple[dict[str, Json], dict[str, Json]]


def cancel_before_start(corpus: EvalCorpus) -> Pair:
    """Cancelled before admission: no model call, no storage read."""
    store, model = hybrid_store(corpus), fake_model()
    composed, _ = runtime(store, model)
    control = composed.use_case.new_control()
    control.cancel()
    asked = ask(composed, briefing_query(Q1), control)
    observed: dict[str, Json] = {
        "error_code": asked.error_code,
        "model_calls": len(model.requests),
        "store_reads": store.reads,
    }
    return {
        "error_code": "synthesis_cancelled",
        "model_calls": 0,
        "store_reads": 0,
    }, observed


def cancel_during_model(corpus: EvalCorpus) -> Pair:
    """Cancelled during the model call: typed cancel, no later reads."""
    store = hybrid_store(corpus)
    holder: dict[str, int] = {}
    control = QueryControl(
        bundled_synthesis_policy().deadlines.request_deadline_seconds
    )

    def cancel(request: SynthesisRequest) -> SynthesisResponse:
        control.cancel()
        holder["reads_at_cancel"] = store.reads
        return draft_response(
            [("East Pier gate closes.", first_span(request, HARBOR) or "")]
        )

    model = scripted_model(cancel)
    composed, _ = runtime(store, model)
    asked = ask(composed, briefing_query(Q1), control)
    observed: dict[str, Json] = {
        "error_code": asked.error_code,
        "model_calls": len(model.requests),
        "store_reads_after_cancel": store.reads
        - holder.get("reads_at_cancel", store.reads),
    }
    return {
        "error_code": "synthesis_cancelled",
        "model_calls": 1,
        "store_reads_after_cancel": 0,
    }, observed


def budget_too_small(corpus: EvalCorpus) -> Pair:
    """A one-token budget fails before retrieval with zero model calls."""
    store, model = hybrid_store(corpus), fake_model()
    composed, _ = runtime(store, model)
    asked = ask(composed, briefing_query(Q1, context_budget=1))
    observed: dict[str, Json] = {
        "error_code": asked.error_code,
        "reason": asked.reason,
        "model_calls": len(model.requests),
    }
    return {
        "error_code": "synthesis_budget_exceeded",
        "reason": "context_too_small",
        "model_calls": 0,
    }, observed


def capacity_full(corpus: EvalCorpus) -> Pair:
    """Full admission capacity rejects without work; release recovers."""
    store, model = hybrid_store(corpus), fake_model()
    composed, _ = runtime(store, model)
    limiter = composed.use_case.limiter
    leases = [limiter.try_acquire() for _ in range(limiter.capacity)]
    asked = ask(composed, briefing_query(Q1))
    calls_while_full = len(model.requests)
    for lease in leases:
        lease.release()
    after = ask(composed, briefing_query(Q1))
    observed: dict[str, Json] = {
        "error_code": asked.error_code,
        "model_calls_while_full": calls_while_full,
        "after_release": after.outcome.route.value
        if after.outcome
        else str(after.error_code),
    }
    return {
        "error_code": "synthesis_concurrency_rejected",
        "model_calls_while_full": 0,
        "after_release": "answered",
    }, observed


def storage_outage(corpus: EvalCorpus) -> Pair:
    """Store outage is typed, keeps documents, and recovers."""
    store, model = hybrid_store(corpus), fake_model()
    composed, _ = runtime(store, model)
    store.available = False
    down = ask(composed, briefing_query(Q1))
    calls_during_outage = len(model.requests)
    retained = len(store.documents)
    store.available = True
    up = ask(composed, briefing_query(Q1))
    observed: dict[str, Json] = {
        "during_outage": down.error_code,
        "model_calls_during_outage": calls_during_outage,
        "documents_retained": retained,
        "after_recovery": up.outcome.route.value if up.outcome else str(up.error_code),
    }
    expected: dict[str, Json] = {
        "during_outage": "retrieval_storage_unavailable",
        "model_calls_during_outage": 0,
        "documents_retained": len(corpus.articles),
        "after_recovery": "answered",
    }
    return expected, observed


def enrichment_outage(corpus: EvalCorpus) -> Pair:
    """Enrichment timeouts publish nothing; lexical search still answers."""
    store = InMemoryRetrievalStore()
    for document in corpus.articles:
        store.add(document)
    world = EnrichmentWorld()
    world.persist(*corpus.articles)
    outcomes = world.drain(FakeTimeoutEnricher())
    published = reindex(store, dict(store.documents), world.published())
    composed, _ = runtime(store, fake_model())
    asked = ask(composed, briefing_query(Q1))
    observed: dict[str, Json] = {
        "enrichment_outcomes": strs(sorted({o.kind.value for o in outcomes})),
        "reindexed": published,
        "route": asked.outcome.route.value if asked.outcome else str(asked.error_code),
        "retrieval_mode": asked.outcome.retrieval.retrieval_mode.value
        if asked.outcome
        else None,
    }
    expected: dict[str, Json] = {
        "enrichment_outcomes": ["RETRY_SCHEDULED"],
        "reindexed": 0,
        "route": "answered",
        "retrieval_mode": "lexical_only",
    }
    return expected, observed


def model_outage(corpus: EvalCorpus) -> Pair:
    """Model outage is typed with no raw fallback answer."""
    observed = failure_summary(hybrid_store(corpus), scripted_model())
    expected: dict[str, Json] = {
        "error_code": "synthesis_provider_failed",
        "answer_returned": False,
        "model_calls": 1,
    }
    return expected, {k: observed[k] for k in expected}
