"""Framework-free briefing workflow boundary (T7).

The compiled briefing graph (graph adapter layer) implements
``BriefingWorkflow`` and calls only the injected ``BriefingSteps``. Callers see
these plain types; no LangGraph type crosses this boundary.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.rag.application.ports.synthesis import AdmissionLease, TokenLedger
from backend.rag.application.use_cases.retrieve import RetrievalDraft
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_scope import ArticleRestriction
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.query_intent import HopStopReason, HopTrace, IntentDecision
from backend.rag.domain.retrieval import QueryControl, RetrievalResult
from backend.rag.domain.synthesis import SynthesisPlan, SynthesisResult


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
    scope: ArticleRestriction | None = None


@dataclass(frozen=True, slots=True)
class BriefingRun:
    """Workflow result: a terminal route plus the result or typed error."""

    route: BriefingRoute
    retrieval: RetrievalResult | None
    result: SynthesisResult | None
    error: Exception | None


@dataclass(frozen=True, slots=True)
class PrimaryHop:
    """Hop-1 draft plus the bounded decision about hop 2."""

    draft: RetrievalDraft
    trace: HopTrace
    next_hop: HopStopReason


@dataclass(frozen=True, slots=True)
class RelatedHop:
    """Hop-2 draft (``None`` when it was skipped) and its trace."""

    draft: RetrievalDraft | None
    trace: HopTrace


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

    def classify(self, invocation: BriefingInvocation) -> IntentDecision:
        """Deterministic query intent (no model call)."""
        ...

    def apply_filters(
        self, invocation: BriefingInvocation, plan: SynthesisPlan
    ) -> RetrievalDraft:
        """Access, publication window and metadata filters (one bounded read)."""
        ...

    def retrieve_primary(
        self,
        invocation: BriefingInvocation,
        intent: IntentDecision,
        draft: RetrievalDraft,
        plan: SynthesisPlan,
    ) -> PrimaryHop:
        """Hop 1 on the user query plus the hop-2 decision."""
        ...

    def retrieve_related(
        self, invocation: BriefingInvocation, primary: PrimaryHop
    ) -> RelatedHop:
        """Bounded hop 2 (one expansion, same filters); never loops."""
        ...

    def fuse(
        self,
        invocation: BriefingInvocation,
        intent: IntentDecision,
        primary: PrimaryHop,
        related: RelatedHop | None,
    ) -> RetrievalDraft:
        """Merge hops and run the single bounded rerank."""
        ...

    def assemble(
        self, invocation: BriefingInvocation, draft: RetrievalDraft
    ) -> RetrievalResult:
        """Final current/tombstone/scope check and budgeted evidence."""
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
