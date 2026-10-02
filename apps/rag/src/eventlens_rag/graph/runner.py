"""Compiled baseline LangGraph skeleton."""

from typing import final
from uuid import uuid4

from langgraph.constants import END, START
from langgraph.graph.state import StateGraph
from langgraph.runtime import Runtime

from eventlens_rag.graph.state import GraphContext, GraphInput, GraphOutput, GraphState


def _record_message(state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
    """Return state tied only to current invocation context."""
    return {"message": state["message"], "request_id": str(runtime.context.request_id)}


@final
class BaselineGraphRunner:
    """Compile and invoke real StateGraph without persistence/checkpointer."""

    def __init__(self) -> None:
        """Compile stateless baseline graph."""
        builder = StateGraph[GraphState, GraphContext, GraphState, GraphState](
            GraphState,
            context_schema=GraphContext,
            input_schema=GraphState,
            output_schema=GraphState,
        )
        _ = builder.add_node("record", _record_message)
        _ = builder.add_edge(START, "record")
        _ = builder.add_edge("record", END)
        self._graph = builder.compile()

    def invoke(self, message: str) -> GraphOutput:
        """Validate input then invoke one isolated graph run."""
        parsed = GraphInput(message=message)
        request_id = uuid4()
        result = self._graph.invoke(
            {"message": parsed.message, "request_id": ""},
            context=GraphContext(request_id=request_id),
        )
        return GraphOutput.model_validate(result)
