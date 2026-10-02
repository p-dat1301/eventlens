"""Real compiled persistence graph with invocation-local input and context."""

from typing import final

from langgraph.constants import END, START
from langgraph.graph.state import StateGraph

from eventlens_rag.application.persist import PersistArticle
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.nodes import PersistenceNode
from eventlens_rag.graph.routes import persistence_route
from eventlens_rag.graph.state import PersistenceChannels, PersistenceState


@final
class PersistenceGraph:
    """Persist-first skeleton with no checkpointer or model nodes."""

    def __init__(self, application: PersistArticle) -> None:
        """Compile injected application service once."""
        builder = StateGraph[
            PersistenceChannels,
            PersistenceContext,
            PersistenceChannels,
            PersistenceChannels,
        ](
            PersistenceChannels,
            context_schema=PersistenceContext,
            input_schema=PersistenceChannels,
            output_schema=PersistenceChannels,
        )
        _ = builder.add_node("persist", PersistenceNode(application))
        _ = builder.add_conditional_edges(
            START, persistence_route, {"persist": "persist", "complete": END}
        )
        _ = builder.add_edge("persist", END)
        self._compiled = builder.compile()

    def invoke(
        self, article: ArticleDocument, context: PersistenceContext
    ) -> PersistenceState:
        """Validate both invocation boundaries, then validate graph output."""
        state = PersistenceState(article=article)
        caller_context = PersistenceContext.model_validate(context)
        return PersistenceState.model_validate(
            self._compiled.invoke(
                {"article": state.article, "persisted": False}, context=caller_context
            )
        )
