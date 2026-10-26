"""Assemble and compile the ingestion StateGraph (no business logic here).

Graph: START -> load -> enrich -> validate -> stage -> publish -> END, with any
typed step failure routed to ``fail`` (one fenced retry/FAILED write) and any
terminal outcome (STALE, CANCELLED, LOST_OWNERSHIP) routed to END. No
checkpointer and no retry loop; one invocation handles exactly one claim.
"""

from itertools import pairwise

from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

from backend.rag.workflows.ingestion.context import EnrichmentContext
from backend.rag.workflows.ingestion.nodes import EnrichmentNodes, Node
from backend.rag.workflows.ingestion.routes import after
from backend.rag.workflows.ingestion.state import EnrichmentChannels

type IngestionGraph = CompiledStateGraph[
    EnrichmentChannels, EnrichmentContext, EnrichmentChannels, EnrichmentChannels
]


def build_ingestion_graph(nodes: EnrichmentNodes) -> IngestionGraph:
    """Add nodes and edges, then compile once."""
    builder = StateGraph[
        EnrichmentChannels,
        EnrichmentContext,
        EnrichmentChannels,
        EnrichmentChannels,
    ](
        EnrichmentChannels,
        context_schema=EnrichmentContext,
        input_schema=EnrichmentChannels,
        output_schema=EnrichmentChannels,
    )
    order: tuple[tuple[str, Node], ...] = (
        ("load", nodes.load),
        ("enrich", nodes.enrich),
        ("validate", nodes.validate),
        ("stage", nodes.stage),
        ("publish", nodes.publish),
        ("fail", nodes.fail),
    )
    for name, node in order:
        _ = builder.add_node(name, node)
    _ = builder.add_edge(START, "load")
    chain = ("load", "enrich", "validate", "stage", "publish")
    for current, following in pairwise(chain):
        _ = builder.add_conditional_edges(
            current,
            after(following),
            {following: following, "fail": "fail", "end": END},
        )
    _ = builder.add_edge("publish", END)
    _ = builder.add_edge("fail", END)
    return builder.compile()
