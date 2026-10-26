"""Assemble and compile the query StateGraph (no business logic here).

Graph: START -> prepare -> retrieve -> (synthesize | abstain) -> answered |
abstained, with every typed step failure routed to exactly one failure
terminal (retrieval_failed, synthesis_failed, timed_out, cancelled,
budget_exceeded, access_denied). Compiled once, no checkpointer.
"""

from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

from backend.rag.workflows.query.context import BriefingContext
from backend.rag.workflows.query.nodes import BriefingNodes, Node, terminal
from backend.rag.workflows.query.routes import FAILURE_ROUTES, after, after_retrieve
from backend.rag.workflows.query.state import BriefingChannels

type QueryGraph = CompiledStateGraph[
    BriefingChannels, BriefingContext, BriefingChannels, BriefingChannels
]


def query_node_order(nodes: BriefingNodes) -> tuple[tuple[str, Node], ...]:
    """Every node name with its callable, in declaration order."""
    return (
        ("prepare", nodes.prepare),
        ("retrieve", nodes.retrieve),
        ("synthesize", nodes.synthesize),
        ("abstain", nodes.synthesize),
        ("answered", nodes.answered),
        ("abstained", nodes.answered),
        *((route.value, terminal(route)) for route in FAILURE_ROUTES),
    )


def build_query_graph(nodes: BriefingNodes) -> QueryGraph:
    """Add nodes and edges, then compile once."""
    builder = StateGraph[
        BriefingChannels, BriefingContext, BriefingChannels, BriefingChannels
    ](
        BriefingChannels,
        context_schema=BriefingContext,
        input_schema=BriefingChannels,
        output_schema=BriefingChannels,
    )
    failures = {route.value: route.value for route in FAILURE_ROUTES}
    for name, node in query_node_order(nodes):
        _ = builder.add_node(name, node)
    _ = builder.add_edge(START, "prepare")
    _ = builder.add_conditional_edges(
        "prepare", after("retrieve"), {"retrieve": "retrieve", **failures}
    )
    _ = builder.add_conditional_edges(
        "retrieve",
        after_retrieve,
        {"synthesize": "synthesize", "abstain": "abstain", **failures},
    )
    _ = builder.add_conditional_edges(
        "synthesize", after("answered"), {"answered": "answered", **failures}
    )
    _ = builder.add_conditional_edges(
        "abstain", after("abstained"), {"abstained": "abstained", **failures}
    )
    for name in ("answered", "abstained", *failures):
        _ = builder.add_edge(name, END)
    return builder.compile()
