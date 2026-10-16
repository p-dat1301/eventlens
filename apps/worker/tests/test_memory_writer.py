from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest

from eventlens_contracts import Event
from eventlens_worker.memory_writer import InMemoryEventWriter
from eventlens_worker.models import (
    EventProposal,
    WriteDisposition,
    WriteResult,
)
from eventlens_worker.ports import EventWriteConflictError, EventWriter

FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures"
ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
PROPOSAL_ID = UUID("d67c8ef5-5685-4182-a8d7-fdb01191433e")


def load_event() -> Event:
    fixture_json = (FIXTURE_DIRECTORY / "event_detected_unknown.v1.json").read_text(
        encoding="utf-8"
    )
    return Event.model_validate_json(fixture_json)


def make_proposal(event: Event) -> EventProposal:
    return EventProposal(
        proposal_id=PROPOSAL_ID,
        article_id=ARTICLE_ID,
        extractor_version="rules-2026.09",
        event=Event.model_validate(event.model_dump() | {"confidence": 0.91}),
    )


def test_write_inserts_new_event() -> None:
    # Given
    writer = InMemoryEventWriter()
    event = load_event()

    # When
    proposal = make_proposal(event)
    result = writer.write(proposal)

    # Then
    assert result == WriteResult(
        disposition=WriteDisposition.INSERTED,
    )
    assert writer.get(event.id) == proposal.event
    assert writer.count == 1


def test_write_returns_unchanged_for_exact_replay() -> None:
    # Given
    writer = InMemoryEventWriter()
    event = load_event()
    _ = writer.write(make_proposal(event))

    # When
    proposal = make_proposal(load_event())
    result = writer.write(proposal)

    # Then
    assert result == WriteResult(
        disposition=WriteDisposition.UNCHANGED,
    )
    assert writer.get(event.id) == proposal.event
    assert writer.count == 1


def test_write_conflicts_when_same_id_has_changed_event() -> None:
    # Given
    writer = InMemoryEventWriter()
    original = load_event()
    changed = original.model_copy(update={"canonical_title": "Changed title"})
    _ = writer.write(make_proposal(original))

    # When
    with pytest.raises(EventWriteConflictError) as captured:
        _ = writer.write(make_proposal(changed))

    # Then
    assert captured.value.event_id == original.id
    assert writer.get(original.id) == make_proposal(original).event
    assert writer.count == 1


def test_get_returns_none_for_unknown_event() -> None:
    # Given
    writer = InMemoryEventWriter()
    unknown_event_id = UUID("811c7e67-1874-4d90-aabe-ce7ea45f4e4c")

    # When
    event = writer.get(unknown_event_id)

    # Then
    assert event is None


def test_write_stores_and_returns_deep_event_snapshots() -> None:
    # Given
    writer = InMemoryEventWriter()
    nested_attributes = {"impact": {"routes": ["A1"]}}
    original = load_event().model_copy(update={"attributes": nested_attributes})
    _ = writer.write(make_proposal(original))

    # When
    nested_attributes["impact"]["routes"].append("B2")
    returned = writer.get(original.id)
    assert returned is not None
    returned_attributes = returned.attributes
    assert returned_attributes is not None
    returned_impact = returned_attributes["impact"]
    assert isinstance(returned_impact, dict)
    returned_routes = returned_impact["routes"]
    assert isinstance(returned_routes, list)
    returned_routes.append("C3")

    # Then
    stored = writer.get(original.id)
    assert stored is not None
    assert stored.attributes == {"impact": {"routes": ["A1"]}}
    with pytest.raises(EventWriteConflictError):
        _ = writer.write(make_proposal(original))


def test_writer_satisfies_event_writer_port() -> None:
    # Given
    writer = InMemoryEventWriter()

    # When / Then
    assert isinstance(writer, EventWriter)


def test_event_write_conflict_error_is_frozen() -> None:
    # Given
    conflict = EventWriteConflictError(event_id=load_event().id)

    # When / Then
    conflict_field = "event_id"
    with pytest.raises(FrozenInstanceError):
        setattr(
            conflict,
            conflict_field,
            UUID("811c7e67-1874-4d90-aabe-ce7ea45f4e4c"),
        )
