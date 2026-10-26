"""Invocation-local runtime context for the ingestion (enrichment) workflow."""

from dataclasses import dataclass
from uuid import UUID

from backend.rag.application.use_cases.enrichment import CancellationScope


@dataclass(frozen=True, slots=True)
class EnrichmentContext:
    """Invocation-local cancellation scope; never stored in graph state."""

    scope: CancellationScope
    invocation_id: UUID
