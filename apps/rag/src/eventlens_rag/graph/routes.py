"""Deterministic baseline route policy."""

from typing import Literal

from eventlens_rag.graph.state import PersistenceChannels


def persistence_route(state: PersistenceChannels) -> Literal["persist", "complete"]:
    """Route only pending state to the persistence boundary."""
    return "complete" if state["persisted"] else "persist"
