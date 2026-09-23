"""In-memory idempotent event writer for local tests."""

from uuid import UUID

from eventlens_contracts import Event
from eventlens_worker.models import EventProposal, WriteDisposition, WriteResult
from eventlens_worker.ports import EventWriteConflictError


class InMemoryEventWriter:
    """Keep mutable event state for local tests; not safe for concurrent use."""

    def __init__(self) -> None:
        """Create an empty local-test event store."""
        self._events: dict[UUID, Event] = {}

    def write(self, proposal: EventProposal) -> WriteResult:
        """Insert event once, report exact replay, or reject changed data."""
        event = proposal.event
        stored = self._events.get(event.id)
        if stored is None:
            self._events[event.id] = event.model_copy(deep=True)
            return WriteResult(
                disposition=WriteDisposition.INSERTED,
            )
        if stored == event:
            return WriteResult(
                disposition=WriteDisposition.UNCHANGED,
            )
        raise EventWriteConflictError(event_id=event.id)

    def get(self, event_id: UUID) -> Event | None:
        """Return stored event for ID when present."""
        stored = self._events.get(event_id)
        return None if stored is None else stored.model_copy(deep=True)

    @property
    def count(self) -> int:
        """Return number of stored events."""
        return len(self._events)
