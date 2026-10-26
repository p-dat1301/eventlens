"""Invocation-local runtime context for the query (briefing) workflow."""

from dataclasses import dataclass

from backend.rag.application.ports.briefing import BriefingInvocation


@dataclass(frozen=True, slots=True)
class BriefingContext:
    """Invocation-local runtime context; never stored in graph state."""

    invocation: BriefingInvocation
