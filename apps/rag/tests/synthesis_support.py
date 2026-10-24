"""Shared T6 synthesis test helpers (fakes + in-memory store; not DB evidence)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from retrieval_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CALLER,
    MemoryStore,
    MutableAccess,
    article,
    briefing,
    control,
    service,
)

from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.application.concurrency import ProcessConcurrencyLimiter
from eventlens_rag.application.ledger import RequestTokenLedger
from eventlens_rag.application.retrieve import RetrievalOptions
from eventlens_rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_synthesis_policy,
)
from eventlens_rag.application.synthesize import PrepareSynthesis, SynthesizeAnswer
from eventlens_rag.domain.activation import ActivationDecision
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.retrieval import QueryControl, RetrievalResult
from eventlens_rag.domain.synthesis import (
    ChatModelIdentity,
    ProviderUsage,
    SynthesisResult,
    SynthesisTelemetryEvent,
    UsageStatus,
)
from eventlens_rag.domain.synthesis_policy import SynthesisPolicy
from eventlens_rag.ports.synthesis import SynthesisModelPort, SynthesisResponse

SYNTH_POLICY = bundled_synthesis_policy()
_ACTIVATION = bundled_activation_policy()
DECISION = ActivationDecision(
    mode="fixture",
    policy_id=_ACTIVATION.policy_id,
    issuer=_ACTIVATION.issuer,
    chat_model=_ACTIVATION.chat_model,
    embedding_model=_ACTIVATION.embedding_model,
    endpoint=None,
    api_key=None,
)
IDENTITY = ChatModelIdentity(
    model_id=_ACTIVATION.chat_model.model_id,
    model_version=_ACTIVATION.chat_model.model_version,
)
COUNTER = FixtureTokenCounter()

__all__ = [
    "CALLER",
    "COUNTER",
    "DECISION",
    "IDENTITY",
    "SYNTH_POLICY",
    "MemoryStore",
    "MutableAccess",
    "article",
    "briefing",
    "control",
]


def sequential_ids() -> Callable[[], UUID]:
    """Deterministic server ID source."""
    state = {"n": 0}

    def next_id() -> UUID:
        state["n"] += 1
        return UUID(int=state["n"])

    return next_id


@dataclass(slots=True)
class RecordingTelemetry:
    events: list[SynthesisTelemetryEvent] = field(
        default_factory=list[SynthesisTelemetryEvent]
    )

    def emit(self, event: SynthesisTelemetryEvent) -> None:
        self.events.append(event)


def store_with(*documents: ArticleDocument) -> MemoryStore:
    store = MemoryStore()
    for document in documents:
        store.add(document)
    return store


def retrieve(
    store: MemoryStore,
    query: BriefingQuery,
    *,
    budget: int | None = None,
    access: MutableAccess | None = None,
) -> RetrievalResult:
    return service(store, access=access).execute(
        query,
        CALLER,
        control(),
        RetrievalOptions(remaining_context_budget=budget),
    )


def synthesizer(
    model: SynthesisModelPort,
    store: MemoryStore,
    *,
    access: MutableAccess | None = None,
    policy: SynthesisPolicy = SYNTH_POLICY,
    telemetry: RecordingTelemetry | None = None,
) -> SynthesizeAnswer:
    return SynthesizeAnswer(
        model=model,
        sources=store,
        access=access or MutableAccess(),
        tokens=COUNTER,
        policy=policy,
        activation=DECISION,
        telemetry=telemetry,
        ids=sequential_ids(),
    )


def ledger(policy: SynthesisPolicy = SYNTH_POLICY) -> RequestTokenLedger:
    return RequestTokenLedger(COUNTER, policy)


def run(  # noqa: PLR0913
    model: SynthesisModelPort,
    store: MemoryStore,
    query: BriefingQuery,
    retrieval: RetrievalResult,
    *,
    access: MutableAccess | None = None,
    request_ledger: RequestTokenLedger | None = None,
    query_control: QueryControl | None = None,
    limiter: ProcessConcurrencyLimiter | None = None,
    telemetry: RecordingTelemetry | None = None,
) -> SynthesisResult:
    """T7 call order for one request: lease -> synthesize -> release."""
    shared = limiter or ProcessConcurrencyLimiter(30)
    lease = shared.try_acquire()
    try:
        return synthesizer(model, store, access=access, telemetry=telemetry).execute(
            query,
            retrieval,
            CALLER,
            query_control or control(),
            request_ledger or ledger(),
            lease,
        )
    finally:
        lease.release()


def prepared_retrieval(
    store: MemoryStore, query: BriefingQuery
) -> tuple[RetrievalResult, RequestTokenLedger]:
    """Prepare -> T5 with the remaining allowance (same counter)."""
    request_ledger = ledger()
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, request_ledger)
    return retrieve(store, query, budget=plan.remaining_context_budget), request_ledger


def claim(
    ref: str,
    text: str,
    spans: list[UUID],
    temporal: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "claim_ref": ref,
        "text": text,
        "evidence_span_ids": [str(span) for span in spans],
        "temporal_claim": temporal,
    }


def draft(
    claims: list[dict[str, object]],
    *,
    status: str = "sufficient",
    uncertainties: list[dict[str, object]] | None = None,
    **extra: object,
) -> str:
    body: dict[str, object] = {
        "schema_version": "synthesis_draft.v1",
        "evidence_status": status,
        "claims": claims,
        "uncertainties": uncertainties or [],
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def respond(
    content: str,
    *,
    usage: ProviderUsage | None = None,
    status: UsageStatus = UsageStatus.MISSING,
    model: str | None = None,
) -> SynthesisResponse:
    return SynthesisResponse(
        content=content,
        usage=usage,
        usage_status=status,
        reported_model=model,
    )


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
