"""Per-invocation channels and runtime context for the briefing graph."""

from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID

from eventlens_rag.domain.retrieval import RetrievalResult
from eventlens_rag.domain.synthesis import SynthesisPlan, SynthesisResult
from eventlens_rag.ports.briefing import BriefingInvocation, BriefingRoute


class BriefingChannels(TypedDict, total=False):
    """Graph channels; nothing persists between invocations (no checkpointer)."""

    invocation_id: UUID
    plan: SynthesisPlan
    retrieval: RetrievalResult
    result: SynthesisResult
    failure: Exception
    route: BriefingRoute


@dataclass(frozen=True, slots=True)
class BriefingContext:
    """Invocation-local runtime context; never stored in graph state."""

    invocation: BriefingInvocation
