"""Validated invocation contexts for persistence and enrichment graphs."""

from dataclasses import dataclass
from uuid import UUID

from eventlens_contracts.base import ContractModel
from eventlens_rag.application.enrichment import CancellationScope
from eventlens_rag.domain.access import CallerScope


class PersistenceContext(ContractModel):
    """Server-owned caller scope, isolated from graph state."""

    caller: CallerScope


@dataclass(frozen=True, slots=True)
class EnrichmentContext:
    """Invocation-local cancellation scope; never stored in graph state."""

    scope: CancellationScope
    invocation_id: UUID
