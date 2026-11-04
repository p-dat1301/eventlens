"""Assemble and compile the summarization StateGraph (no business logic here).

Graph: START -> load -> prepare -> summarize -> validate -> verify_current ->
publish -> END; a typed failure routes to ``fail`` and a terminal outcome
(STALE, lost fence) to END. No checkpointer; one invocation = one claim.
"""

from itertools import pairwise

from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

from backend.rag.workflows.summarization.context import SummaryContext
from backend.rag.workflows.summarization.nodes import Node, SummaryNodes
from backend.rag.workflows.summarization.routes import after
from backend.rag.workflows.summarization.state import SummaryChannels

type SummarizationGraph = CompiledStateGraph[
    SummaryChannels, SummaryContext, SummaryChannels, SummaryChannels
]

CHAIN = ("load", "prepare", "summarize", "validate", "verify_current", "publish")


def build_summarization_graph(nodes: SummaryNodes) -> SummarizationGraph:
    """Add nodes and edges, then compile once."""
    builder = StateGraph[
        SummaryChannels, SummaryContext, SummaryChannels, SummaryChannels
    ](
        SummaryChannels,
        context_schema=SummaryContext,
        input_schema=SummaryChannels,
        output_schema=SummaryChannels,
    )
    order: tuple[tuple[str, Node], ...] = (
        ("load", nodes.load),
        ("prepare", nodes.prepare),
        ("summarize", nodes.summarize),
        ("validate", nodes.validate),
        ("verify_current", nodes.verify_current),
        ("publish", nodes.publish),
        ("fail", nodes.fail),
    )
    for name, node in order:
        _ = builder.add_node(name, node)
    _ = builder.add_edge(START, "load")
    for current, following in pairwise(CHAIN):
        _ = builder.add_conditional_edges(
            current,
            after(following),
            {following: following, "fail": "fail", "end": END},
        )
    _ = builder.add_edge("publish", END)
    _ = builder.add_edge("fail", END)
    return builder.compile()
