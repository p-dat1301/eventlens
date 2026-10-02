"""Persistence-only graph node; no enrichment or model invocation."""

from dataclasses import dataclass

from langgraph.runtime import Runtime

from eventlens_rag.application.persist import PersistArticle
from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.state import PersistenceChannels, PersistenceState


@dataclass(frozen=True, slots=True)
class PersistenceNode:
    """Inject framework-free application service into graph runtime."""

    application: PersistArticle

    def __call__(
        self, state: PersistenceChannels, runtime: Runtime[PersistenceContext]
    ) -> PersistenceChannels:
        """Persist validated input using current invocation caller."""
        context = PersistenceContext.model_validate(runtime.context)
        parsed = PersistenceState.model_validate(state)
        self.application.execute(parsed.article, context.caller)
        return {"article": parsed.article, "persisted": True}
