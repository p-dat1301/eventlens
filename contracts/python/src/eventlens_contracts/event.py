"""Event v1 canonical current-state contract."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import (
    ContractModel,
    NonEmptyText,
    NonNegativeInt32,
    UtcDatetime,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

BoundedConfidence = Annotated[float, Field(ge=0, le=1)]
EVENT_ENTITY_ID_MISMATCH = "event_entity_id_mismatch"
DUPLICATE_EVENT_ENTITY = "duplicate_event_entity"


class EventEntity(ContractModel):
    """Canonical Entity identity joined with EventEntity relation fields."""

    event_id: UUID
    entity_id: UUID
    entity_type: Literal[
        "LOCATION", "ORG", "PERSON", "SERVICE", "PRODUCT", "FACILITY", "OTHER"
    ]
    canonical_name: NonEmptyText
    role: Literal["primary", "affected", "involved", "mentioned"]
    confidence: BoundedConfidence
    first_linked_at: UtcDatetime
    is_active: bool


class Event(ContractModel):
    """Canonical current state of event version 1."""

    id: UUID
    event_type: Literal[
        "TRAFFIC_DISRUPTION",
        "SERVICE_OUTAGE",
        "WEATHER_EVENT",
        "PUBLIC_SAFETY",
        "POLICY_ANNOUNCEMENT",
        "SCHEDULED_EVENT",
    ]
    canonical_title: NonEmptyText
    status: Literal[
        "detected",
        "unconfirmed",
        "confirmed",
        "ongoing",
        "resolved",
        "cancelled",
        "merged",
    ]
    severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    time_start: UtcDatetime | None = None
    time_end: UtcDatetime | None = None
    time_precision: Literal["exact", "day", "range", "unknown"] | None = None
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    last_changed_at: UtcDatetime
    source_count: NonNegativeInt32
    article_count: NonNegativeInt32
    confidence: BoundedConfidence | None = None
    attributes: JsonObject | None = None
    schema_version: Literal["event.v1"]
    created_at: UtcDatetime
    updated_at: UtcDatetime
    entities: tuple[EventEntity, ...]

    @model_validator(mode="after")
    def enforce_entity_relations(self) -> Self:
        """Reject relation rows belonging elsewhere or duplicating Event keys."""
        if any(entity.event_id != self.id for entity in self.entities):
            message = "event entity event_id must match event id"
            raise PydanticCustomError(EVENT_ENTITY_ID_MISMATCH, message)
        entity_roles = tuple(
            (entity.entity_id, entity.role) for entity in self.entities
        )
        if len(set(entity_roles)) != len(entity_roles):
            message = "event entity_id and role pairs must be unique"
            raise PydanticCustomError(DUPLICATE_EVENT_ENTITY, message)
        return self
