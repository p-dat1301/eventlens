"""Deterministic briefing route policy (no I/O, no framework types)."""

from collections.abc import Callable

from eventlens_rag.domain.retrieval import (
    EmptyReason,
    ResultStatus,
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalDeadlineExceededError,
    RetrievalError,
)
from eventlens_rag.domain.synthesis import SynthesisOutcome
from eventlens_rag.domain.synthesis_errors import (
    SynthesisAccessDeniedError,
    SynthesisBudgetError,
    SynthesisCancelledError,
    SynthesisDeadlineExceededError,
    SynthesisError,
)
from eventlens_rag.graph.briefing_state import BriefingChannels
from eventlens_rag.ports.briefing import BriefingRoute

FAILURE_ROUTES = (
    BriefingRoute.RETRIEVAL_FAILED,
    BriefingRoute.SYNTHESIS_FAILED,
    BriefingRoute.TIMED_OUT,
    BriefingRoute.CANCELLED,
    BriefingRoute.BUDGET_EXCEEDED,
    BriefingRoute.ACCESS_DENIED,
)
type StepError = RetrievalError | SynthesisError


def failure_route(error: StepError) -> BriefingRoute:
    """Classify one typed step failure; the error itself is kept unchanged."""
    match error:
        case RetrievalCancelledError() | SynthesisCancelledError():
            return BriefingRoute.CANCELLED
        case RetrievalDeadlineExceededError() | SynthesisDeadlineExceededError():
            return BriefingRoute.TIMED_OUT
        case SynthesisBudgetError():
            return BriefingRoute.BUDGET_EXCEEDED
        case RetrievalAccessDeniedError() | SynthesisAccessDeniedError():
            return BriefingRoute.ACCESS_DENIED
        case RetrievalError():
            return BriefingRoute.RETRIEVAL_FAILED
        case SynthesisError():
            return BriefingRoute.SYNTHESIS_FAILED


def _failed(state: BriefingChannels) -> str | None:
    failure = state.get("failure")
    if failure is None:
        return None
    assert isinstance(failure, RetrievalError | SynthesisError)  # noqa: S101
    return failure_route(failure).value


def after(next_node: str) -> Callable[[BriefingChannels], str]:
    """Route to the failure terminal if the step failed, else ``next_node``."""

    def route(state: BriefingChannels) -> str:
        return _failed(state) or next_node

    return route


def after_retrieve(state: BriefingChannels) -> str:
    """Empty evidence goes to abstention; a too-small budget stays a failure.

    ``CONTEXT_BUDGET_TOO_SMALL`` still goes through synthesis, which raises the
    typed budget error (T6 outcome mapping); it is never an abstention.
    """
    failed = _failed(state)
    if failed is not None:
        return failed
    retrieval = state.get("retrieval")
    if (
        retrieval is not None
        and retrieval.status is ResultStatus.EMPTY
        and retrieval.empty_reason is not EmptyReason.CONTEXT_BUDGET_TOO_SMALL
    ):
        return "abstain"
    return "synthesize"


def answered_route(outcome: SynthesisOutcome) -> BriefingRoute:
    """Terminal route for a successful synthesis result."""
    match outcome:
        case SynthesisOutcome.ANSWERED:
            return BriefingRoute.ANSWERED
        case SynthesisOutcome.ABSTAINED_BY_MODEL:
            return BriefingRoute.ABSTAINED_BY_MODEL
        case SynthesisOutcome.ABSTAINED_NO_EVIDENCE:
            return BriefingRoute.ABSTAINED_NO_EVIDENCE
