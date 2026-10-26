# ruff: noqa: EM101

"""Query workflow facade (T7): compile once, one isolated invocation per request.

Graph shape lives in ``graph.py``; nodes in ``nodes.py``; routing in
``routes.py``. Satisfies the ``BriefingWorkflow`` application port.
"""

from typing import final

from backend.rag.application.ports.briefing import (
    BriefingInvocation,
    BriefingRun,
    BriefingSteps,
)
from backend.rag.domain.retrieval import RetrievalError
from backend.rag.domain.synthesis_errors import SynthesisError
from backend.rag.workflows.query.context import BriefingContext
from backend.rag.workflows.query.graph import build_query_graph, query_node_order
from backend.rag.workflows.query.nodes import BriefingGraphStateError, BriefingNodes
from backend.rag.workflows.query.routes import failure_route

__all__ = ["BriefingGraph", "BriefingGraphStateError"]


@final
class BriefingGraph:
    """Compiled briefing workflow; satisfies ``BriefingWorkflow``."""

    def __init__(self, steps: BriefingSteps) -> None:
        """Compile the graph once over injected application steps."""
        nodes = BriefingNodes(steps)
        self._compiled = build_query_graph(nodes)
        self.node_names: tuple[str, ...] = tuple(
            name for name, _ in query_node_order(nodes)
        )

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
