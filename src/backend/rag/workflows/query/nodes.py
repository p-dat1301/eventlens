# ruff: noqa: EM101

"""Query nodes: read state, call one ``BriefingSteps`` method, return a patch.

No SQL, provider or HTTP code lives here.
"""

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.runtime import Runtime

from backend.rag.application.ports.briefing import (
    BriefingRoute,
    BriefingSteps,
    PrimaryHop,
)
from backend.rag.application.use_cases.retrieve import RetrievalDraft
from backend.rag.domain.query_intent import IntentDecision
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


def _intent(state: BriefingChannels) -> IntentDecision:
    value = state.get("intent")
    if value is None:
        raise BriefingGraphStateError("intent")
    return value


def _draft(state: BriefingChannels, channel: str) -> RetrievalDraft:
    value = state.get("fused") if channel == "fused" else state.get("filtered")
    if value is None:
        raise BriefingGraphStateError(channel)
    return value


def _primary(state: BriefingChannels) -> PrimaryHop:
    value = state.get("primary")
    if value is None:
        raise BriefingGraphStateError("primary")
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

    def classify_intent(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Deterministic intent (rules only, no model)."""
        del state
        try:
            return {"intent": self.steps.classify(runtime.context.invocation)}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def apply_filters(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Access, publication window and metadata filters."""
        invocation = runtime.context.invocation
        try:
            return {"filtered": self.steps.apply_filters(invocation, _plan(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def retrieve_primary(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Hop 1 and the bounded hop-2 decision."""
        invocation = runtime.context.invocation
        try:
            primary = self.steps.retrieve_primary(
                invocation, _intent(state), _draft(state, "filtered"), _plan(state)
            )
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}
        return {"primary": primary}

    def retrieve_related(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Optional bounded hop 2 (runs at most once; no loop edge exists)."""
        invocation = runtime.context.invocation
        try:
            return {"related": self.steps.retrieve_related(invocation, _primary(state))}
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}

    def fuse(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Merge hops; single bounded rerank."""
        invocation = runtime.context.invocation
        try:
            fused = self.steps.fuse(
                invocation, _intent(state), _primary(state), state.get("related")
            )
        except (RetrievalError, SynthesisError) as error:
            return {"failure": error}
        return {"fused": fused}

    def assemble_evidence(
        self, state: BriefingChannels, runtime: Runtime[BriefingContext]
    ) -> BriefingChannels:
        """Final current-revision check and the budgeted evidence context."""
        invocation = runtime.context.invocation
        try:
            return {
                "retrieval": self.steps.assemble(invocation, _draft(state, "fused"))
            }
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
