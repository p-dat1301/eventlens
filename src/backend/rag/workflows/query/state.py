"""Per-invocation channels of the query (briefing) workflow."""

from typing import TypedDict
from uuid import UUID

from backend.rag.application.ports.briefing import BriefingRoute
from backend.rag.domain.retrieval import RetrievalResult
from backend.rag.domain.synthesis import SynthesisPlan, SynthesisResult


class BriefingChannels(TypedDict, total=False):
    """Graph channels; nothing persists between invocations (no checkpointer)."""

    invocation_id: UUID
    plan: SynthesisPlan
    retrieval: RetrievalResult
    result: SynthesisResult
    failure: Exception
    route: BriefingRoute
