"""Evidence-backed temporal claim contract."""

# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import ContractModel, NonEmptyText, UtcDatetime

BoundedConfidence = Annotated[float, Field(ge=0, le=1)]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class TemporalClaim(ContractModel):
    """Known temporal value or explicit unknown, always evidence-backed."""

    value_state: Literal["known", "unknown"]
    start_at: UtcDatetime | None
    end_at: UtcDatetime | None
    precision: Literal["minute", "hour", "day", "month", "year", "unknown"]
    confidence: BoundedConfidence
    evidence_span_ids: tuple[UUID, ...]
    relative_expression: NonEmptyText | None
    reference_at: UtcDatetime | None
    timezone_basis: Literal["UTC"] | None

    @model_validator(mode="after")
    def enforce_temporal_basis(self) -> Self:
        """Prevent unsupported event-time substitution or relative-time ambiguity."""
        if not self.evidence_span_ids:
            raise _error("empty_temporal_evidence", "temporal claims require evidence")
        if len(set(self.evidence_span_ids)) != len(self.evidence_span_ids):
            raise _error(
                "duplicate_temporal_evidence",
                "temporal evidence span ids must be unique",
            )
        match self.value_state:
            case "known":
                if self.start_at is None:
                    raise _error(
                        "known_temporal_value_missing_timestamp",
                        "known value needs start_at",
                    )
            case "unknown":
                if self.start_at is not None or self.end_at is not None:
                    raise _error(
                        "unknown_temporal_value_has_timestamp",
                        "unknown value cannot carry timestamps",
                    )
        if (
            self.end_at is not None
            and self.start_at is not None
            and self.end_at < self.start_at
        ):
            raise _error("invalid_temporal_range", "end_at must not precede start_at")
        has_relative_basis = (
            self.reference_at is not None and self.timezone_basis == "UTC"
        )
        if self.relative_expression is not None and not has_relative_basis:
            raise _error(
                "relative_temporal_basis_required",
                "relative values require UTC reference_at and timezone_basis",
            )
        if self.relative_expression is None and (
            self.reference_at is not None or self.timezone_basis is not None
        ):
            raise _error(
                "unexpected_temporal_basis", "only relative values carry a clock basis"
            )
        return self
