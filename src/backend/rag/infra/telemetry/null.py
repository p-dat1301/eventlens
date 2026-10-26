"""Telemetry sink that drops every briefing event."""

from dataclasses import dataclass

from backend.rag.application.ports.telemetry import BriefingTelemetryEvent


@dataclass(frozen=True, slots=True)
class NullBriefingTelemetry:
    """Drop every event."""

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Ignore ``event``."""
        del event
