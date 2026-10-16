"""Telemetry sinks for job outcomes (identifiers and codes only)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from eventlens_rag.domain.enrichment_lifecycle import JobOutcome


@dataclass(slots=True)
class RecordingTelemetry:  # noqa: MUTABLE_OK
    """Keep outcomes in memory for tests and CLI summaries."""

    outcomes: list[JobOutcome] = field(default_factory=list)

    def emit(self, outcome: JobOutcome) -> None:
        """Append one outcome."""
        self.outcomes.append(outcome)


@dataclass(frozen=True, slots=True)
class JsonLinesTelemetry:
    """Write one JSON object per outcome through an injected line writer."""

    write: Callable[[str], None]

    def emit(self, outcome: JobOutcome) -> None:
        """Serialize the telemetry-safe record."""
        self.write(json.dumps({"event": "enrichment_job", **outcome.as_record()}))
