"""Framework-free briefing workflow boundary (T7).

The compiled briefing graph (graph adapter layer) implements
``BriefingWorkflow`` and calls only the injected ``BriefingSteps``. Callers see
these plain types; no LangGraph type crosses this boundary.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.retrieval import QueryControl, RetrievalResult
from eventlens_rag.domain.synthesis import SynthesisPlan, SynthesisResult
from eventlens_rag.ports.synthesis import AdmissionLease, TokenLedger


class BriefingRoute(StrEnum):
    """Terminal route of one briefing invocation."""

    ANSWERED = "answered"
    ABSTAINED_NO_EVIDENCE = "abstained_no_evidence"
    ABSTAINED_BY_MODEL = "abstained_by_model"
    RETRIEVAL_FAILED = "retrieval_failed"
    SYNTHESIS_FAILED = "synthesis_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"
    ACCESS_DENIED = "access_denied"


@dataclass(frozen=True, slots=True)
class BriefingInvocation:
    """Per-request inputs; built by the server, never from the request body."""

    invocation_id: UUID
    query: BriefingQuery
    caller: CallerScope
    control: QueryControl
    ledger: TokenLedger
    lease: AdmissionLease


@dataclass(frozen=True, slots=True)
class BriefingRun:
    """Workflow result: a terminal route plus the result or typed error."""

    route: BriefingRoute
    retrieval: RetrievalResult | None
    result: SynthesisResult | None
    error: Exception | None


class BriefingSteps(Protocol):
    """Application services the graph nodes may call (and nothing else)."""

    def prepare(self, invocation: BriefingInvocation) -> SynthesisPlan:
        """Admit, check access and plan the evidence budget (no I/O)."""
        ...

    def retrieve(
        self, invocation: BriefingInvocation, plan: SynthesisPlan
    ) -> RetrievalResult:
        """Run T5 retrieval within the planned evidence budget."""
        ...

    def synthesize(
        self, invocation: BriefingInvocation, retrieval: RetrievalResult
    ) -> SynthesisResult:
        """Run T6 synthesis (abstains without a model call on empty evidence)."""
        ...


class BriefingWorkflow(Protocol):
    """One isolated briefing invocation from query to typed result."""

    def run(self, invocation: BriefingInvocation) -> BriefingRun:
        """Execute the workflow; typed failures are returned, not raised."""
        ...
