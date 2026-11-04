"""Pure routing for the summarization workflow (no I/O, no framework types)."""

from collections.abc import Callable

from backend.rag.workflows.summarization.state import SummaryChannels


def after(next_node: str) -> Callable[[SummaryChannels], str]:
    """Terminal outcome -> END, typed failure -> ``fail``, else ``next_node``."""

    def route(state: SummaryChannels) -> str:
        if "outcome" in state:
            return "end"
        if "failure" in state:
            return "fail"
        return next_node

    return route
