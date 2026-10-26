"""Briefing telemetry port: codes, counts and latency only."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BriefingTelemetryEvent:
    """Codes, counts and latency only; never query, prompt or content."""

    invocation_id: UUID
    route: str
    error_code: str | None
    reason: str | None
    model_calls: int
    latency_ms: float


class BriefingTelemetry(Protocol):
    """Telemetry sink for briefing invocations."""

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Record one event."""
        ...
