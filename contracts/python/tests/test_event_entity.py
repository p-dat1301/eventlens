import json

import pytest
from pydantic import ValidationError

from eventlens_contracts import EventEntity
from eventlens_contracts.event import JsonValue

ENTITY_TYPES = [
    "LOCATION",
    "ORG",
    "PERSON",
    "SERVICE",
    "PRODUCT",
    "FACILITY",
    "OTHER",
]


def entity_payload(entity_type: str) -> dict[str, JsonValue]:
    return {
        "event_id": "59da2c4c-872f-4a63-b16c-bf19dcbd85d0",
        "entity_id": "48b5d66f-5dc6-4363-b2df-2066ad355c94",
        "entity_type": entity_type,
        "canonical_name": "Mỹ Đình",
        "role": "primary",
        "confidence": 0.9,
        "first_linked_at": "2026-09-14T08:30:00Z",
        "is_active": True,
    }


@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_event_entity_accepts_registered_entity_type(entity_type: str) -> None:
    # Given
    payload = entity_payload(entity_type)

    # When
    entity = EventEntity.model_validate_json(json.dumps(payload))

    # Then
    assert entity.entity_type == entity_type


def test_event_entity_rejects_unregistered_entity_type() -> None:
    # Given
    payload = entity_payload("PLACE")

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = EventEntity.model_validate_json(json.dumps(payload))
    assert captured.value.errors()[0]["type"] == "literal_error"


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_event_entity_rejects_confidence_outside_unit_interval(
    confidence: float,
) -> None:
    # Given
    payload = entity_payload("LOCATION")
    payload["confidence"] = confidence

    # When / Then
    with pytest.raises(ValidationError):
        _ = EventEntity.model_validate_json(json.dumps(payload))
