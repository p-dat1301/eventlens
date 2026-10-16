from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens_contracts import Event
from eventlens_worker.bundle import ArticleBundle
from eventlens_worker.models import (
    EventProposal,
    GateDecision,
    GateReason,
    GateVerdict,
    WriteDisposition,
    WriteResult,
)
from eventlens_worker.ports import EventExtractor, EventWriter

FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures"
ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
PROPOSAL_ID = UUID("d67c8ef5-5685-4182-a8d7-fdb01191433e")


def load_bundle() -> ArticleBundle:
    fixture_json = (FIXTURE_DIRECTORY / "article_bundle.v1.json").read_text(
        encoding="utf-8"
    )
    return ArticleBundle.model_validate_json(fixture_json)


def load_event() -> Event:
    fixture_json = (FIXTURE_DIRECTORY / "event_detected_unknown.v1.json").read_text(
        encoding="utf-8"
    )
    return Event.model_validate_json(fixture_json)


def make_proposal() -> EventProposal:
    return EventProposal(
        proposal_id=PROPOSAL_ID,
        article_id=ARTICLE_ID,
        extractor_version="rules-2026.09",
        event=Event.model_validate(load_event().model_dump() | {"confidence": 0.91}),
    )


def test_gate_types_expose_complete_wire_values() -> None:
    # Given / When
    verdict_values = tuple(verdict.value for verdict in GateVerdict)
    reason_values = tuple(reason.value for reason in GateReason)
    disposition_values = tuple(disposition.value for disposition in WriteDisposition)

    # Then
    assert verdict_values == ("ACCEPT", "REJECT", "REVIEW")
    assert reason_values == (
        "article_id_mismatch",
        "duplicate_article",
        "low_confidence",
        "flagged_input",
        "missing_event_time",
        "ready",
    )
    assert disposition_values == ("inserted", "unchanged")


def test_event_proposal_preserves_typed_event_and_adapter_version() -> None:
    # Given
    event = Event.model_validate(load_event().model_dump() | {"confidence": 0.75})

    # When
    proposal = EventProposal(
        proposal_id=PROPOSAL_ID,
        article_id=ARTICLE_ID,
        extractor_version="local-rules-v17",
        event=event,
    )

    # Then
    assert proposal.event is event
    assert proposal.extractor_version == "local-rules-v17"
    assert proposal.model_config.get("frozen") is True
    assert proposal.model_config.get("strict") is True
    assert proposal.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_event_proposal_rejects_confidence_outside_unit_interval(
    confidence: float,
) -> None:
    # Given
    payload = {
        "proposal_id": PROPOSAL_ID,
        "article_id": ARTICLE_ID,
        "extractor_version": "rules-2026.09",
        "event": load_event().model_dump() | {"confidence": confidence},
    }

    # When / Then
    with pytest.raises(ValidationError, match="confidence"):
        _ = EventProposal.model_validate(payload)


def test_event_proposal_rejects_coercion_and_extra_fields() -> None:
    # Given
    payload = {
        "proposal_id": str(PROPOSAL_ID),
        "article_id": ARTICLE_ID,
        "extractor_version": "rules-2026.09",
        "event": load_event().model_dump() | {"confidence": 0.8},
        "unexpected": True,
    }

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = EventProposal.model_validate(payload)
    assert {error["type"] for error in captured.value.errors()} == {
        "is_instance_of",
        "extra_forbidden",
    }


def test_event_proposal_rejects_missing_event_confidence() -> None:
    # Given
    event = load_event()

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = EventProposal(
            proposal_id=PROPOSAL_ID,
            article_id=ARTICLE_ID,
            extractor_version="rules-2026.09",
            event=event,
        )
    assert captured.value.errors()[0]["type"] == "proposal_event_confidence_required"


def test_event_proposal_rejects_blank_extractor_version() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError, match="extractor_version"):
        _ = EventProposal(
            proposal_id=PROPOSAL_ID,
            article_id=ARTICLE_ID,
            extractor_version=" ",
            event=Event.model_validate(
                load_event().model_dump() | {"confidence": 0.91}
            ),
        )


def test_decision_and_write_result_are_frozen_values() -> None:
    # Given
    decision = GateDecision(
        verdict=GateVerdict.REVIEW,
        reasons=(GateReason.FLAGGED_INPUT, GateReason.MISSING_EVENT_TIME),
    )
    result = WriteResult(
        disposition=WriteDisposition.UNCHANGED,
    )

    # When / Then
    decision_field = "verdict"
    result_field = "disposition"
    with pytest.raises(FrozenInstanceError):
        setattr(decision, decision_field, GateVerdict.ACCEPT)
    with pytest.raises(FrozenInstanceError):
        setattr(result, result_field, WriteDisposition.INSERTED)


def test_ports_accept_structural_sync_adapters() -> None:
    # Given
    proposal = make_proposal()

    class StubExtractor:
        def extract(self, bundle: ArticleBundle) -> EventProposal:
            assert bundle.article.article_id == proposal.article_id
            return proposal

    class StubWriter:
        def write(self, _proposal: EventProposal) -> WriteResult:
            return WriteResult(
                disposition=WriteDisposition.INSERTED,
            )

    extractor = StubExtractor()
    writer = StubWriter()

    # When
    extracted = extractor.extract(load_bundle())
    result = writer.write(extracted)

    # Then
    assert isinstance(extractor, EventExtractor)
    assert isinstance(writer, EventWriter)
    assert extracted is proposal
    assert result == WriteResult(
        disposition=WriteDisposition.INSERTED,
    )
