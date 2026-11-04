"""Summarization workflow facade: compile-once graph and the bounded pass."""

from contextlib import suppress
from dataclasses import dataclass
from typing import Final, final
from uuid import uuid4

from backend.rag.application.use_cases.summarize import SummarySteps
from backend.rag.domain.summary import SummaryOutcome
from backend.rag.domain.work_jobs import ClaimedWorkJob
from backend.rag.workflows.summarization.context import SummaryContext
from backend.rag.workflows.summarization.graph import build_summarization_graph
from backend.rag.workflows.summarization.nodes import GraphStateError, SummaryNodes

__all__ = [
    "GraphStateError",
    "SummaryGraph",
    "SummaryPass",
    "SummaryRunner",
]

MAX_JOBS_PER_PASS: Final = 25


class SummaryOutcomeMissingError(RuntimeError):
    """Graph ended without a terminal outcome (programming error)."""


@final
class SummaryGraph:
    """Compiled StateGraph for one claimed summary job; no checkpointer."""

    def __init__(self, steps: SummarySteps) -> None:
        """Compile injected application steps once."""
        self._compiled = build_summarization_graph(SummaryNodes(steps))

    def invoke(self, job: ClaimedWorkJob, worker_id: str) -> SummaryOutcome:
        """Run one isolated invocation."""
        result = self._compiled.invoke(
            {"job": job},
            context=SummaryContext(invocation_id=uuid4(), worker_id=worker_id),
        )
        outcome = result.get("outcome")
        if outcome is None:
            raise SummaryOutcomeMissingError
        return outcome


@dataclass(frozen=True, slots=True)
class SummaryPass:
    """Bounded pass summary."""

    outcomes: tuple[SummaryOutcome, ...]


@final
class SummaryRunner:
    """Worker boundary: claim and summarize at most N jobs, one at a time."""

    def __init__(self, steps: SummarySteps) -> None:
        """Compile the graph once for every pass."""
        self._steps = steps
        self._graph = SummaryGraph(steps)

    def run(self, max_jobs: int, worker_id: str) -> SummaryPass:
        """Process up to ``max_jobs`` due summary jobs."""
        if not 1 <= max_jobs <= MAX_JOBS_PER_PASS:
            msg = f"max_jobs must be between 1 and {MAX_JOBS_PER_PASS}"
            raise ValueError(msg)
        outcomes: list[SummaryOutcome] = []
        while len(outcomes) < max_jobs:
            claimed = self._steps.claim(worker_id, 1)
            if not claimed:
                break
            job = claimed[0]
            try:
                outcomes.append(self._graph.invoke(job, worker_id))
            except Exception:
                # Storage outage mid-job: release the claim with backoff (one
                # attempt consumed) instead of waiting for the lease, then
                # let the supervised worker loop log and back off.
                with suppress(Exception):
                    _ = self._steps.queue.retry(
                        job.fence,
                        "storage_unavailable",
                        self._steps.policy.backoff(job.attempt),
                    )
                raise
        return SummaryPass(tuple(outcomes))
