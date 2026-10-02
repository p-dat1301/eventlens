import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from eventlens_contracts import Event
from eventlens_contracts.event import JsonValue

FIXTURE = Path(__file__).parents[3] / "fixtures/event_detected_unknown.v1.json"


def event_payload() -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_json(
        FIXTURE.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("field", ["source_count", "article_count"])
def test_event_rejects_count_above_postgresql_integer(field: str) -> None:
    # Given
    payload = event_payload()
    payload[field] = 2_147_483_648

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = Event.model_validate_json(json.dumps(payload))
    assert captured.value.errors()[0]["type"] == "less_than_equal"


@pytest.mark.parametrize("attributes", ["scalar", ["list"]])
def test_event_rejects_non_object_attributes(attributes: JsonValue) -> None:
    # Given
    payload = event_payload()
    payload["attributes"] = attributes

    # When / Then
    with pytest.raises(ValidationError) as captured:
        _ = Event.model_validate_json(json.dumps(payload))
    assert captured.value.errors()[0]["type"] == "dict_type"


def test_event_rejects_blank_canonical_title() -> None:
    # Given
    payload = event_payload()
    payload["canonical_title"] = "   "

    # When / Then
    with pytest.raises(ValidationError):
        _ = Event.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_event_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    # Given
    payload = event_payload()
    payload["confidence"] = confidence

    # When / Then
    with pytest.raises(ValidationError):
        _ = Event.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("fixture_name", "time_start", "time_end"),
    [
        (
            "event_resolved_day.v1.json",
            "2026-09-12T17:00:00Z",
            "2026-09-13T16:59:00Z",
        ),
        (
            "event_cancelled.v1.json",
            "2026-09-30T17:00:00Z",
            "2026-10-01T16:59:00Z",
        ),
    ],
)
def test_vietnamese_day_fixture_uses_utc_boundaries(
    fixture_name: str, time_start: str, time_end: str
) -> None:
    # Given
    fixture = FIXTURE.parents[0] / fixture_name

    # When
    event = Event.model_validate_json(fixture.read_text(encoding="utf-8"))

    # Then
    assert event.time_start is not None
    assert event.time_end is not None
    assert event.time_start.isoformat().replace("+00:00", "Z") == time_start
    assert event.time_end.isoformat().replace("+00:00", "Z") == time_end
