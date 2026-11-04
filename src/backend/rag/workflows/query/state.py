"""Per-invocation channels of the query (briefing) workflow."""

from typing import TypedDict
from uuid import UUID

from backend.rag.application.ports.briefing import (
    BriefingRoute,
    PrimaryHop,
    RelatedHop,
)
from backend.rag.application.use_cases.retrieve import RetrievalDraft
from backend.rag.domain.query_intent import IntentDecision
from backend.rag.domain.retrieval import RetrievalResult
from backend.rag.domain.synthesis import SynthesisPlan, SynthesisResult


class BriefingChannels(TypedDict, total=False):
    """Graph channels; nothing persists between invocations (no checkpointer)."""

    invocation_id: UUID
    plan: SynthesisPlan
    intent: IntentDecision
    filtered: RetrievalDraft
    primary: PrimaryHop
    related: RelatedHop
    fused: RetrievalDraft
    retrieval: RetrievalResult
    result: SynthesisResult
    failure: Exception
    route: BriefingRoute
