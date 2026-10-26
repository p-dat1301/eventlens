"""Pure routing for the ingestion workflow (no I/O, no framework types)."""

from collections.abc import Callable

from backend.rag.workflows.ingestion.state import EnrichmentChannels


def after(next_node: str) -> Callable[[EnrichmentChannels], str]:
    """Route a terminal outcome to END, a typed failure to ``fail``, else on."""

    def route(state: EnrichmentChannels) -> str:
        if "outcome" in state:
            return "end"
        if "failure" in state:
            return "fail"
        return next_node

    return route
