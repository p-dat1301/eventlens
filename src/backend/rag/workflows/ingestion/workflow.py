"""Ingestion workflow facade: compile-once graph and the bounded worker pass.

Graph shape lives in ``graph.py``; nodes in ``nodes.py``; routing in
``routes.py``. This module only invokes the compiled graph per claim.
"""

from dataclasses import dataclass
from typing import final
from uuid import uuid4

from backend.rag.application.ports.enrichment import TelemetrySink
from backend.rag.application.use_cases.enrichment import (
    CancellationScope,
    EnrichmentSteps,
    InvocationCancelledError,
)
from backend.rag.domain.enrichment_lifecycle import ClaimedJob, JobOutcome
from backend.rag.workflows.ingestion.context import EnrichmentContext
from backend.rag.workflows.ingestion.graph import build_ingestion_graph
from backend.rag.workflows.ingestion.nodes import EnrichmentNodes, GraphStateError

__all__ = [
    "EnrichmentGraph",
    "EnrichmentRunner",
    "GraphOutcomeMissingError",
    "GraphStateError",
    "InvalidPassSizeError",
    "RunReport",
]


class InvalidPassSizeError(ValueError):
    """Requested job count is outside the versioned per-pass limit."""

    def __init__(self, requested: int, limit: int) -> None:
        """Name the requested and allowed counts."""
        super().__init__(f"max_jobs must be between 1 and {limit}: {requested}")


class GraphOutcomeMissingError(RuntimeError):
    """Graph ended without a terminal outcome (programming error)."""


@final
class EnrichmentGraph:
    """Compiled StateGraph for one claimed job; no checkpointer."""

    def __init__(self, steps: EnrichmentSteps) -> None:
        """Compile injected application steps once."""
        self._compiled = build_ingestion_graph(EnrichmentNodes(steps))

    def invoke(self, claim: ClaimedJob, scope: CancellationScope) -> JobOutcome:
        """Run one isolated invocation and return its terminal outcome."""
        result = self._compiled.invoke(
            {"claim": claim},
            context=EnrichmentContext(scope=scope, invocation_id=uuid4()),
        )
        outcome = result.get("outcome")
        if outcome is None:
            raise GraphOutcomeMissingError
        return outcome


@dataclass(frozen=True, slots=True)
class RunReport:
    """Bounded pass summary."""

    reconciled: int
    outcomes: tuple[JobOutcome, ...]
    cancelled: bool


@final
class EnrichmentRunner:
    """Worker boundary: reconcile, then claim and process at most N jobs."""

    def __init__(self, steps: EnrichmentSteps, telemetry: TelemetrySink) -> None:
        """Compile the graph once for every pass."""
        self._steps = steps
        self._telemetry = telemetry
        self._graph = EnrichmentGraph(steps)

    def run(self, max_jobs: int, scope: CancellationScope | None = None) -> RunReport:
        """Process up to ``max_jobs`` (bounded by policy) eligible jobs."""
        limit = self._steps.policy.max_jobs_per_pass
        if not 1 <= max_jobs <= limit:
            raise InvalidPassSizeError(max_jobs, limit)
        scope = scope or CancellationScope()
        try:
            reconciled = scope.serialized(lambda: self._steps.reconcile(limit))
        except InvocationCancelledError:
            return RunReport(0, (), cancelled=True)
        outcomes: list[JobOutcome] = []
        while len(outcomes) < max_jobs and not scope.stopped:
            claim = self._steps.claim_into(scope)
            if claim is None:
                break
            outcome = self._graph.invoke(claim, scope)
            self._telemetry.emit(outcome)
            outcomes.append(outcome)
        return RunReport(reconciled, tuple(outcomes), cancelled=scope.stopped)
