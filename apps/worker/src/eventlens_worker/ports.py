"""Structural ports implemented by worker adapters."""

from dataclasses import dataclass
from typing import Protocol, override, runtime_checkable
from uuid import UUID

from eventlens_worker.bundle import ArticleBundle
from eventlens_worker.models import EventProposal, WriteResult


@dataclass(frozen=True, slots=True)
class EventWriteConflictError(Exception):
    """Report an event ID already bound to different event data."""

    event_id: UUID

    @override
    def __str__(self) -> str:
        """Return conflict message containing event identity."""
        return f"event {self.event_id} conflicts with stored event"


@runtime_checkable
class EventExtractor(Protocol):
    """Extract an event proposal from a validated article bundle."""

    def extract(self, bundle: ArticleBundle) -> EventProposal:
        """Return proposal produced from bundle."""
        ...


@runtime_checkable
class EventWriter(Protocol):
    """Persist an accepted event proposal idempotently."""

    def write(self, proposal: EventProposal) -> WriteResult:
        """Return observable persistence outcome."""
        ...
