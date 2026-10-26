# ruff: noqa: EM101

"""Query nodes: read state, call one ``BriefingSteps`` method, return a patch.

No SQL, provider or HTTP code lives here.
"""

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.runtime import Runtime

from backend.rag.application.ports.briefing import BriefingRoute, BriefingSteps
from backend.rag.domain.retrieval import RetrievalError, RetrievalResult
from backend.rag.domain.synthesis import SynthesisPlan
from backend.rag.domain.synthesis_errors import SynthesisError
from backend.rag.workflows.query.context import BriefingContext
from backend.rag.workflows.query.routes import answered_route
from backend.rag.workflows.query.state import BriefingChannels

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
class BriefingNodes:
    """Node callables bound to the injected application steps."""

    steps: BriefingSteps

    def prepare(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Plan the synthesis budget for this invocation."""
        del state
        try:
            return {"plan": self.steps.prepare(runtime.context.invocation)}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def retrieve(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Retrieve and assemble the evidence context."""
        invocation = runtime.context.invocation
        try:
            return {"retrieval": self.steps.retrieve(invocation, _plan(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def synthesize(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Synthesize and validate a grounded answer (also used by ``abstain``)."""
        invocation = runtime.context.invocation
        try:
            return {"result": self.steps.synthesize(invocation, _retrieval(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    @staticmethod
    def answered(
        state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Finalize a successful result with its terminal route."""
        del runtime
        result = state.get("result")
        if result is None:
            raise BriefingGraphStateError("result")
        return {"route": answered_route(result.outcome)}


def terminal(route: BriefingRoute) -> Node:
    """Failure terminal node that only records its route."""

    def node(
        state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        del state, runtime
        return {"route": route}

    return node
