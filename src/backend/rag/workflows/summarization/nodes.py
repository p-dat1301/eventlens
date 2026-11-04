# ruff: noqa: EM101

"""Summarization nodes: read state, call one ``SummarySteps`` method, patch."""

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.runtime import Runtime

from backend.rag.application.ports.summary import SummaryModelResponse
from backend.rag.application.use_cases.summarize import (
    SummaryStepFailure,
    SummarySteps,
)
from backend.rag.domain.summary import (
    SummaryFailureCode,
    SummaryOutcome,
    SummaryResult,
    SummarySource,
)
from backend.rag.domain.work_jobs import ClaimedWorkJob
from backend.rag.workflows.summarization.context import SummaryContext
from backend.rag.workflows.summarization.state import SummaryChannels

type Node = Callable[[SummaryChannels, Runtime[SummaryContext]], SummaryChannels]


class GraphStateError(RuntimeError):
    """A node ran without the channel its predecessor must have produced."""

    def __init__(self, channel: str) -> None:
        """Name the missing channel."""
        super().__init__(f"summary graph channel missing: {channel}")


def _job(state: SummaryChannels) -> ClaimedWorkJob:
    value = state.get("job")
    if value is None:
        raise GraphStateError("job")
    return value


def _source(state: SummaryChannels) -> SummarySource:
    value = state.get("source")
    if value is None:
        raise GraphStateError("source")
    return value


def _response(state: SummaryChannels) -> SummaryModelResponse:
    value = state.get("response")
    if value is None:
        raise GraphStateError("response")
    return value


def _result(state: SummaryChannels) -> SummaryResult:
    value = state.get("result")
    if value is None:
        raise GraphStateError("result")
    return value


def _failure(state: SummaryChannels) -> SummaryFailureCode:
    value = state.get("failure")
    if value is None:
        raise GraphStateError("failure")
    return value


def _terminal(result: object) -> SummaryChannels | None:
    if isinstance(result, SummaryOutcome):
        return {"outcome": result}
    if isinstance(result, SummaryStepFailure):
        return {"failure": result.code}
    return None


@dataclass(frozen=True, slots=True)
class SummaryNodes:
    """Node callables bound to the injected application steps."""

    steps: SummarySteps

    def load(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """Load exactly the claimed revision."""
        del runtime
        result = self.steps.load(_job(state))
        terminal = _terminal(result)
        if terminal is not None:
            return terminal
        assert isinstance(result, SummarySource)  # noqa: S101
        return {"source": result}

    def prepare(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """Check the input budget (no truncation)."""
        del runtime
        result = self.steps.prepare(_source(state))
        terminal = _terminal(result)
        if terminal is not None:
            return terminal
        assert isinstance(result, SummarySource)  # noqa: S101
        return {"source": result}

    def summarize(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """One model call."""
        del runtime
        result = self.steps.summarize(_source(state))
        terminal = _terminal(result)
        if terminal is not None:
            return terminal
        assert isinstance(result, SummaryModelResponse)  # noqa: S101
        return {"response": result}

    def validate(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """Strict draft and exact spans."""
        del runtime
        result = self.steps.validate(_source(state), _response(state))
        terminal = _terminal(result)
        if terminal is not None:
            return terminal
        assert isinstance(result, SummaryResult)  # noqa: S101
        return {"result": result}

    def verify_current(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """The revision must still be current after the model call."""
        del runtime
        outcome = self.steps.verify_current(_job(state))
        return {} if outcome is None else {"outcome": outcome}

    def publish(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """Fenced publication."""
        del runtime
        return {
            "outcome": self.steps.publish(_job(state), _source(state), _result(state))
        }

    def fail(
        self, state: SummaryChannels, runtime: Runtime[SummaryContext]
    ) -> SummaryChannels:
        """One fenced retry or FAILED write."""
        del runtime
        return {"outcome": self.steps.fail(_job(state), _failure(state))}
