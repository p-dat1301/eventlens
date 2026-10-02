import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from eventlens_contracts import Event, EventEntity
from eventlens_contracts.event import JsonValue

FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures"
EVENT_FIXTURES = (
    "event_detected_unknown.v1.json",
    "event_unconfirmed_optional.v1.json",
    "event_confirmed_exact.v1.json",
    "event_ongoing_range.v1.json",
    "event_resolved_day.v1.json",
    "event_cancelled.v1.json",
    "event_merged.v1.json",
)
EVENT_TYPES = (
    "TRAFFIC_DISRUPTION",
    "SERVICE_OUTAGE",
    "WEATHER_EVENT",
    "PUBLIC_SAFETY",
    "POLICY_ANNOUNCEMENT",
    "SCHEDULED_EVENT",
)
EVENT_STATUSES = (
    "detected",
    "unconfirmed",
    "confirmed",
    "ongoing",
    "resolved",
    "cancelled",
    "merged",
)
ENTITY_TYPES = {
    "LOCATION",
    "ORG",
    "PERSON",
    "SERVICE",
    "PRODUCT",
    "FACILITY",
    "OTHER",
}


def fixture_payload(
    name: str = "event_detected_unknown.v1.json",
) -> dict[str, JsonValue]:
    fixture_json = (FIXTURE_DIRECTORY / name).read_text(encoding="utf-8")
    return TypeAdapter(dict[str, JsonValue]).validate_json(fixture_json)


def validate_payload(payload: dict[str, JsonValue]) -> Event:
    return Event.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("fixture_name", EVENT_FIXTURES)
def test_event_validates_golden_fixture(fixture_name: str) -> None:
    # Given
    fixture_json = (FIXTURE_DIRECTORY / fixture_name).read_text(encoding="utf-8")

    # When
    event = Event.model_validate_json(fixture_json)

    # Then
    assert event.schema_version == "event.v1"


def test_golden_fixtures_cover_all_statuses() -> None:
    # Given / When
    statuses = {
        validate_payload(fixture_payload(name)).status for name in EVENT_FIXTURES
    }

    # Then
    assert statuses == set(EVENT_STATUSES)


def test_golden_fixtures_cover_all_entity_types() -> None:
    # Given / When
    entity_types = {
        entity.entity_type
        for name in EVENT_FIXTURES
        for entity in validate_payload(fixture_payload(name)).entities
    }

    # Then
    assert entity_types == ENTITY_TYPES


def test_event_contract_has_exact_fields() -> None:
    # Given / When
    fields = set(Event.model_fields)

    # Then
    assert fields == {
        "id",
        "event_type",
        "canonical_title",
        "status",
        "severity",
        "time_start",
        "time_end",
        "time_precision",
        "first_seen_at",
        "last_seen_at",
        "last_changed_at",
        "source_count",
        "article_count",
        "confidence",
        "attributes",
        "schema_version",
        "created_at",
        "updated_at",
        "entities",
    }


def test_event_entity_contract_has_exact_fields() -> None:
    # Given / When
    fields = set(EventEntity.model_fields)

    # Then
    assert fields == {
        "event_id",
        "entity_id",
        "entity_type",
        "canonical_name",
        "role",
        "confidence",
        "first_linked_at",
        "is_active",
    }


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_event_accepts_registered_event_type(event_type: str) -> None:
    # Given
    payload = fixture_payload()
    payload["event_type"] = event_type

    # When / Then
    _ = validate_payload(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("event_type", "wildfire"),
        ("status", "updated"),
        ("severity", "urgent"),
        ("time_precision", "minute"),
    ],
)
def test_event_rejects_unregistered_enum(field: str, invalid_value: str) -> None:
    # Given
    payload = fixture_payload()
    payload[field] = invalid_value

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = validate_payload(payload)
    assert captured.value.errors()[0]["type"] == "literal_error"


def test_event_accepts_omitted_optional_fields() -> None:
    # Given
    payload = fixture_payload("event_unconfirmed_optional.v1.json")
    optional_fields = {
        "severity",
        "time_start",
        "time_end",
        "time_precision",
        "confidence",
        "attributes",
    }

    # When
    event = validate_payload(payload)

    # Then
    assert optional_fields.isdisjoint(payload)
    assert all(getattr(event, field) is None for field in optional_fields)
    assert (event.source_count, event.article_count) == (2, 2)


def test_event_accepts_zero_counts() -> None:
    # Given
    payload = fixture_payload()
    payload["source_count"] = 0
    payload["article_count"] = 0

    # When
    event = validate_payload(payload)

    # Then
    assert (event.source_count, event.article_count) == (0, 0)


def test_event_keeps_time_fields_independently_optional() -> None:
    # Given
    payload = fixture_payload("event_confirmed_exact.v1.json")
    payload["time_end"] = payload["time_start"]
    payload["time_precision"] = None

    # When
    event = validate_payload(payload)

    # Then
    assert event.time_start == event.time_end
    assert event.time_precision is None


def test_event_accepts_recursive_json_attributes() -> None:
    # Given
    payload = fixture_payload()
    payload["attributes"] = {
        "districts": ["Ba Dinh", "Cau Giay"],
        "official": True,
        "lane_count": 3,
        "impact": {"delay_minutes": 20.5, "note": None},
    }

    # When
    event = validate_payload(payload)

    # Then
    assert event.attributes == payload["attributes"]


def test_event_is_frozen() -> None:
    # Given
    event = validate_payload(fixture_payload())

    # When / Then
    with pytest.raises(ValidationError) as captured:
        event.status = "resolved"
    assert captured.value.errors()[0]["type"] == "frozen_instance"


def test_event_entity_strict_model_rejects_string_event_id() -> None:
    # Given
    payload_with_string_event_id = {
        "event_id": "59da2c4c-872f-4a63-b16c-bf19dcbd85d0",
        "entity_id": UUID("48b5d66f-5dc6-4363-b2df-2066ad355c94"),
        "entity_type": "LOCATION",
        "canonical_name": "Mỹ Đình",
        "role": "primary",
        "confidence": 0.9,
        "first_linked_at": "2026-09-14T08:30:00Z",
        "is_active": True,
    }

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = EventEntity.model_validate(payload_with_string_event_id)
    assert captured.value.errors()[0]["type"] == "is_instance_of"


@pytest.mark.parametrize("role", ["primary", "affected", "involved", "mentioned"])
def test_event_entity_accepts_registered_role(role: str) -> None:
    # Given
    fixture_json = (FIXTURE_DIRECTORY / "event_detected_unknown.v1.json").read_text(
        encoding="utf-8"
    )
    payload = fixture_json.replace('"role": "primary"', f'"role": "{role}"')

    # When / Then
    _ = Event.model_validate_json(payload)
