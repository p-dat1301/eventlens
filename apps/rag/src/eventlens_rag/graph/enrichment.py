"""Real compiled enrichment StateGraph and its bounded worker runner.

Graph: START -> load -> enrich -> validate -> stage -> publish -> END, with any
typed step failure routed to ``fail`` (one fenced retry/FAILED write) and any
terminal outcome (STALE, CANCELLED, LOST_OWNERSHIP) routed to END. The graph has
no checkpointer and no retry loop; one invocation handles exactly one claim.
"""

# ruff: noqa: EM101

from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, TypedDict, final
from uuid import uuid4

from langgraph.constants import END, START
from langgraph.graph.state import StateGraph
from langgraph.runtime import Runtime

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.application.enrichment import (
    CancellationScope,
    EnrichmentSteps,
    InvocationCancelledError,
    StepFailure,
)
from eventlens_rag.application.index import StagedGeneration
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.enrichment_draft import EnrichmentDraft
from eventlens_rag.domain.enrichment_lifecycle import (
    ClaimedJob,
    FailureCode,
    JobOutcome,
)
from eventlens_rag.graph.context import EnrichmentContext
from eventlens_rag.ports.enrichment import TelemetrySink


class EnrichmentChannels(TypedDict, total=False):
    """Per-invocation channels; nothing persists between invocations."""

    claim: ClaimedJob
    article: ArticleDocument
    draft: EnrichmentDraft
    metadata: ArticleEnrichment
    staged: StagedGeneration
    failure: FailureCode
    outcome: JobOutcome


class InvalidPassSizeError(ValueError):
    """Requested job count is outside the versioned per-pass limit."""

    def __init__(self, requested: int, limit: int) -> None:
        """Name the requested and allowed counts."""
        super().__init__(f"max_jobs must be between 1 and {limit}: {requested}")


class GraphOutcomeMissingError(RuntimeError):
    """Graph ended without a terminal outcome (programming error)."""


type Node = Callable[
    [EnrichmentChannels, Runtime[EnrichmentContext]], EnrichmentChannels
]


class GraphStateError(RuntimeError):
    """A node ran without the channel its predecessor must have produced."""

    def __init__(self, channel: str) -> None:
        """Name the missing channel."""
        super().__init__(f"enrichment graph channel missing: {channel}")


def _claim(state: EnrichmentChannels) -> ClaimedJob:
    value = state.get("claim")
    if value is None:
        raise GraphStateError("claim")
    return value


def _article(state: EnrichmentChannels) -> ArticleDocument:
    value = state.get("article")
    if value is None:
        raise GraphStateError("article")
    return value


def _draft(state: EnrichmentChannels) -> EnrichmentDraft:
    value = state.get("draft")
    if value is None:
        raise GraphStateError("draft")
    return value


def _metadata(state: EnrichmentChannels) -> ArticleEnrichment:
    value = state.get("metadata")
    if value is None:
        raise GraphStateError("metadata")
    return value


def _staged(state: EnrichmentChannels) -> StagedGeneration:
    value = state.get("staged")
    if value is None:
        raise GraphStateError("staged")
    return value


def _failure(state: EnrichmentChannels) -> FailureCode:
    value = state.get("failure")
    if value is None:
        raise GraphStateError("failure")
    return value


def _route(
    result: object, success_key: Literal["article", "draft", "metadata", "staged"]
) -> EnrichmentChannels:
    if isinstance(result, JobOutcome):
        return {"outcome": result}
    if isinstance(result, StepFailure):
        return {"failure": result.code}
    update: EnrichmentChannels = {}
    match success_key:
        case "article":
            assert isinstance(result, ArticleDocument)  # noqa: S101
            update["article"] = result
        case "draft":
            assert isinstance(result, EnrichmentDraft)  # noqa: S101
            update["draft"] = result
        case "metadata":
            assert isinstance(result, ArticleEnrichment)  # noqa: S101
            update["metadata"] = result
        case "staged":
            assert isinstance(result, StagedGeneration)  # noqa: S101
            update["staged"] = result
    return update


@dataclass(frozen=True, slots=True)
class _Nodes:
    steps: EnrichmentSteps

    def load(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        return _route(self.steps.load(_claim(state), runtime.context.scope), "article")

    def enrich(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        result = self.steps.enrich(
            _claim(state), _article(state), runtime.context.scope
        )
        return _route(result, "draft")

    def validate(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        result = self.steps.validate(
            _claim(state), _article(state), _draft(state), runtime.context.scope
        )
        return _route(result, "metadata")

    def stage(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        result = self.steps.stage(_claim(state), _article(state), runtime.context.scope)
        return _route(result, "staged")

    def publish(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        outcome = self.steps.publish(
            _claim(state), _staged(state), _metadata(state), runtime.context.scope
        )
        return {"outcome": outcome}

    def fail(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        outcome = self.steps.fail(_claim(state), _failure(state), runtime.context.scope)
        return {"outcome": outcome}


def _after(next_node: str) -> Callable[[EnrichmentChannels], str]:
    def route(state: EnrichmentChannels) -> str:
        if "outcome" in state:
            return "end"
        if "failure" in state:
            return "fail"
        return next_node

    return route


@final
class EnrichmentGraph:
    """Compiled StateGraph for one claimed job; no checkpointer."""

    def __init__(self, steps: EnrichmentSteps) -> None:
        """Compile injected application steps once."""
        nodes = _Nodes(steps)
        builder = StateGraph[
            EnrichmentChannels,
            EnrichmentContext,
            EnrichmentChannels,
            EnrichmentChannels,
        ](
            EnrichmentChannels,
            context_schema=EnrichmentContext,
            input_schema=EnrichmentChannels,
            output_schema=EnrichmentChannels,
        )
        order: tuple[tuple[str, Node], ...] = (
            ("load", nodes.load),
            ("enrich", nodes.enrich),
            ("validate", nodes.validate),
            ("stage", nodes.stage),
            ("publish", nodes.publish),
            ("fail", nodes.fail),
        )
        for name, node in order:
            _ = builder.add_node(name, node)
        _ = builder.add_edge(START, "load")
        chain = ("load", "enrich", "validate", "stage", "publish")
        for current, following in pairwise(chain):
            _ = builder.add_conditional_edges(
                current,
                _after(following),
                {following: following, "fail": "fail", "end": END},
            )
        _ = builder.add_edge("publish", END)
        _ = builder.add_edge("fail", END)
        self._compiled = builder.compile()

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
