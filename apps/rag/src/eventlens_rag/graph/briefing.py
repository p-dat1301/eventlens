# ruff: noqa: EM101

"""Real compiled briefing StateGraph (T7).

Graph: START -> prepare -> retrieve -> (synthesize | abstain) -> answered |
abstained, with every typed step failure routed to exactly one failure
terminal (retrieval_failed, synthesis_failed, timed_out, cancelled,
budget_exceeded, access_denied). Nodes call only the injected
``BriefingSteps``; no SQL, provider or HTTP code lives here. Compiled once,
no checkpointer, one isolated invocation per request.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import final

from langgraph.constants import END, START
from langgraph.graph.state import StateGraph
from langgraph.runtime import Runtime

from eventlens_rag.domain.retrieval import RetrievalError, RetrievalResult
from eventlens_rag.domain.synthesis import SynthesisPlan
from eventlens_rag.domain.synthesis_errors import SynthesisError
from eventlens_rag.graph.briefing_routes import (
    FAILURE_ROUTES,
    after,
    after_retrieve,
    answered_route,
    failure_route,
)
from eventlens_rag.graph.briefing_state import BriefingChannels, BriefingContext
from eventlens_rag.ports.briefing import (
    BriefingInvocation,
    BriefingRoute,
    BriefingRun,
    BriefingSteps,
)

type Node = Callable[[BriefingChannels, Runtime[BriefingContext]], BriefingChannels]


class BriefingGraphStateError(RuntimeError):
    """A node ran without the channel its predecessor must have produced."""

    def __init__(self, channel: str) -> None:
        """Name the missing channel."""
        super().__init__(f"briefing graph channel missing: {channel}")


def _plan(state: BriefingChannels) -> SynthesisPlan:
    value = state.get("plan")
    if value is None:
        raise BriefingGraphStateError("plan")
    return value


def _retrieval(state: BriefingChannels) -> RetrievalResult:
    value = state.get("retrieval")
    if value is None:
        raise BriefingGraphStateError("retrieval")
    return value


@dataclass(frozen=True, slots=True)
class _Nodes:
    steps: BriefingSteps

    def prepare(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        del state
        try:
            return {"plan": self.steps.prepare(runtime.context.invocation)}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def retrieve(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        invocation = runtime.context.invocation
        try:
            return {"retrieval": self.steps.retrieve(invocation, _plan(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def synthesize(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        invocation = runtime.context.invocation
        try:
            return {"result": self.steps.synthesize(invocation, _retrieval(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    @staticmethod
    def answered(
        state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        del runtime
        result = state.get("result")
        if result is None:
            raise BriefingGraphStateError("result")
        return {"route": answered_route(result.outcome)}


def _terminal(route: BriefingRoute) -> Node:
    def node(
        state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        del state, runtime
        return {"route": route}

    return node


@final
class BriefingGraph:
    """Compiled briefing workflow; satisfies ``BriefingWorkflow``."""

    def __init__(self, steps: BriefingSteps) -> None:
        """Compile the graph once over injected application steps."""
        nodes = _Nodes(steps)
        builder = StateGraph[
            BriefingChannels, BriefingContext, BriefingChannels, BriefingChannels
        ](
            BriefingChannels,
            context_schema=BriefingContext,
            input_schema=BriefingChannels,
            output_schema=BriefingChannels,
        )
        failures = {route.value: route.value for route in FAILURE_ROUTES}
        order: tuple[tuple[str, Node], ...] = (
            ("prepare", nodes.prepare),
            ("retrieve", nodes.retrieve),
            ("synthesize", nodes.synthesize),
            ("abstain", nodes.synthesize),
            ("answered", nodes.answered),
            ("abstained", nodes.answered),
            *((route.value, _terminal(route)) for route in FAILURE_ROUTES),
        )
        for name, node in order:
            _ = builder.add_node(name, node)
        _ = builder.add_edge(START, "prepare")
        _ = builder.add_conditional_edges(
            "prepare", after("retrieve"), {"retrieve": "retrieve", **failures}
        )
        _ = builder.add_conditional_edges(
            "retrieve",
            after_retrieve,
            {"synthesize": "synthesize", "abstain": "abstain", **failures},
        )
        _ = builder.add_conditional_edges(
            "synthesize", after("answered"), {"answered": "answered", **failures}
        )
        _ = builder.add_conditional_edges(
            "abstain", after("abstained"), {"abstained": "abstained", **failures}
        )
        for terminal in ("answered", "abstained", *failures):
            _ = builder.add_edge(terminal, END)
        self._compiled = builder.compile()
        self.node_names: tuple[str, ...] = tuple(name for name, _ in order)

    def run(self, invocation: BriefingInvocation) -> BriefingRun:
        """Run one isolated invocation; typed failures are returned."""
        state = self._compiled.invoke(
            {"invocation_id": invocation.invocation_id},
            context=BriefingContext(invocation=invocation),
        )
        route = state.get("route")
        if route is None:
            raise BriefingGraphStateError("route")
        failure = state.get("failure")
        if failure is not None:
            assert isinstance(failure, RetrievalError | SynthesisError)  # noqa: S101
            if failure_route(failure) is not route:
                raise BriefingGraphStateError("route")
        return BriefingRun(
            route=route,
            retrieval=state.get("retrieval"),
            result=state.get("result"),
            error=failure,
        )
