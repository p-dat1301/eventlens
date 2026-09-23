"""Bounded synchronous orchestration for worker pipeline adapters."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import assert_never, final, override
from uuid import UUID

from eventlens_contracts import ArticleBundle
from eventlens_worker.gate import decide_event
from eventlens_worker.models import GateVerdict, ProcessingTrace
from eventlens_worker.ports import EventExtractor, EventWriteConflictError, EventWriter


@dataclass(frozen=True, slots=True)
class RunnerPorts:
    """Adapters required by pipeline runner."""

    extractor: EventExtractor
    writer: EventWriter


@dataclass(frozen=True, slots=True)
class InvalidRunnerLimitError(Exception):
    """Report non-positive pipeline batch limit."""

    limit: int

    @override
    def __str__(self) -> str:
        """Return message containing invalid limit."""
        return f"runner limit must be at least 1, got {self.limit}"


@dataclass(frozen=True, slots=True)
class BatchLimitExceededError(Exception):
    """Report input batch larger than configured limit."""

    limit: int
    received: int

    @override
    def __str__(self) -> str:
        """Return message containing limit and received count."""
        return f"batch limit {self.limit} exceeded by {self.received} received items"


@dataclass(frozen=True, slots=True)
class BatchAbortedError(Exception):
    """Report prior completed items when a writer conflict aborts a batch."""

    completed: tuple[ProcessingTrace, ...]
    event_id: UUID

    @override
    def __str__(self) -> str:
        """Return stable batch failure summary."""
        return f"batch aborted after {len(self.completed)} completed items"


@final
class PipelineRunner:
    """Process validated bundles synchronously through bounded adapter calls."""

    def __init__(self, ports: RunnerPorts, max_items: int) -> None:
        """Create runner after validating positive batch limit."""
        if max_items < 1:
            raise InvalidRunnerLimitError(limit=max_items)
        self._ports = ports
        self._max_items = max_items

    def run(self, bundles: Sequence[ArticleBundle]) -> tuple[ProcessingTrace, ...]:
        """Process bundles in order after checking whole batch size."""
        received = len(bundles)
        if received > self._max_items:
            raise BatchLimitExceededError(limit=self._max_items, received=received)

        traces: list[ProcessingTrace] = []
        for bundle in bundles:
            proposal = self._ports.extractor.extract(bundle)
            decision = decide_event(bundle, proposal)
            match decision.verdict:
                case GateVerdict.ACCEPT:
                    try:
                        write_result = self._ports.writer.write(proposal)
                    except EventWriteConflictError as error:
                        raise BatchAbortedError(
                            completed=tuple(traces),
                            event_id=error.event_id,
                        ) from error
                    traces.append(
                        ProcessingTrace(
                            schema_version="processing_trace.v1",
                            article_id=bundle.article.article_id,
                            proposal_id=proposal.proposal_id,
                            event_id=proposal.event.id,
                            verdict=decision.verdict,
                            reasons=decision.reasons,
                            write_disposition=write_result.disposition,
                        )
                    )
                    continue
                case GateVerdict.REJECT | GateVerdict.REVIEW:
                    traces.append(
                        ProcessingTrace(
                            schema_version="processing_trace.v1",
                            article_id=bundle.article.article_id,
                            proposal_id=proposal.proposal_id,
                            event_id=proposal.event.id,
                            verdict=decision.verdict,
                            reasons=decision.reasons,
                            write_disposition=None,
                        )
                    )
                    continue
            assert_never(decision.verdict)
        return tuple(traces)
