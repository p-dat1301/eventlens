"""Query graph: intent classification and the bounded second hop."""

import time
from collections.abc import Sequence
from typing import Protocol, cast

from backend.rag.composition import BriefingRuntime, compose_briefing, fixture_store
from backend.rag.domain.query_intent import HopPolicy, HopStopReason
from backend.rag.domain.retrieval import QueryControl
from backend.rag.workflows.query.graph import build_query_graph
from backend.rag.workflows.query.nodes import BriefingNodes
from briefing_support import CLOCK, query

UPDATE_QUESTION = "What is the latest update on the East Pier closure?"
FACTUAL_QUESTION = "Which authority closed East Pier for repairs?"


class _Edge(Protocol):
    source: str
    target: str


class _Graph(Protocol):
    edges: Sequence[_Edge]


class _GraphLike(Protocol):
    def get_graph(self) -> _Graph:
        """Return the introspection graph."""
        ...


def _runtime(*, enabled: bool) -> BriefingRuntime:
    return compose_briefing(
        fixture_store(), clock=lambda: CLOCK, hops=HopPolicy(enabled=enabled)
    )


def test_update_intent_runs_exactly_one_extra_hop_when_enabled() -> None:
    runtime = _runtime(enabled=True)
    outcome = runtime.use_case.execute(query(UPDATE_QUESTION))
    provenance = outcome.retrieval.provenance
    assert provenance.query_intent == "update"
    assert [h.hop for h in provenance.hops] == [1, 2]
    assert provenance.hops[1].decision is HopStopReason.ALLOWED
    assert len(provenance.hops) <= 2


def test_factual_intent_never_takes_a_second_hop() -> None:
    runtime = _runtime(enabled=True)
    outcome = runtime.use_case.execute(query(FACTUAL_QUESTION))
    provenance = outcome.retrieval.provenance
    assert provenance.query_intent == "factual"
    assert [h.hop for h in provenance.hops] == [1]


def test_hops_are_off_by_default_and_recorded() -> None:
    runtime = _runtime(enabled=False)
    outcome = runtime.use_case.execute(query(UPDATE_QUESTION))
    hops = outcome.retrieval.provenance.hops
    assert [h.hop for h in hops] == [1]


def test_deadline_near_skips_the_second_hop() -> None:
    runtime = _runtime(enabled=True)
    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    control = QueryControl(9.0, monotonic=monotonic)
    clock[0] = 2.0  # 7 s left < 8 s minimum for hop 2
    outcome = runtime.use_case.execute(query(UPDATE_QUESTION), control=control)
    assert [h.hop for h in outcome.retrieval.provenance.hops] == [1]


def test_graph_has_no_back_edge_so_retrieval_cannot_loop() -> None:
    runtime = _runtime(enabled=True)
    graph = build_query_graph(BriefingNodes(runtime.use_case.services))
    introspection = cast("_GraphLike", cast("object", graph))
    edges: set[tuple[str, str]] = {
        (edge.source, edge.target) for edge in introspection.get_graph().edges
    }
    order = [
        "prepare",
        "classify_intent",
        "apply_filters",
        "retrieve_primary",
        "retrieve_related",
        "fuse",
        "assemble_evidence",
    ]
    position = {name: index for index, name in enumerate(order)}
    for source, target in edges:
        if source in position and target in position:
            assert position[target] > position[source], (source, target)


def test_second_hop_respects_wall_clock_bounds() -> None:
    runtime = _runtime(enabled=True)
    started = time.perf_counter()
    for _ in range(5):
        _ = runtime.use_case.execute(query(UPDATE_QUESTION))
    assert time.perf_counter() - started < 10
