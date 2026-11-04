"""Invocation-local runtime context for the summarization workflow."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SummaryContext:
    """Invocation identity; never stored in graph state."""

    invocation_id: UUID
    worker_id: str
