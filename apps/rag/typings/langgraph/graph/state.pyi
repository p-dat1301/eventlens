from collections.abc import Callable

from langgraph.runtime import Runtime

from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.state import GraphContext

type ConditionalRoute[StateT] = Callable[[StateT], str]

class CompiledStateGraph[StateT, ContextT, InputT, OutputT]:
    checkpointer: None
    def invoke(self, payload: InputT, /, *, context: ContextT) -> OutputT: ...

class StateGraph[StateT, ContextT: GraphContext | PersistenceContext, InputT, OutputT]:
    def __init__(
        self,
        state_schema: type[StateT],
        context_schema: type[ContextT],
        *,
        input_schema: type[InputT],
        output_schema: type[OutputT],
    ) -> None: ...
    def add_node(
        self,
        node: str,
        action: Callable[[StateT, Runtime[ContextT]], StateT],
    ) -> StateGraph[StateT, ContextT, InputT, OutputT]: ...
    def add_conditional_edges(
        self,
        source: str,
        path: ConditionalRoute[StateT],
        path_map: dict[str, str],
    ) -> StateGraph[StateT, ContextT, InputT, OutputT]: ...
    def add_edge(
        self, start_key: str, end_key: str
    ) -> StateGraph[StateT, ContextT, InputT, OutputT]: ...
    def compile(self) -> CompiledStateGraph[StateT, ContextT, InputT, OutputT]: ...
