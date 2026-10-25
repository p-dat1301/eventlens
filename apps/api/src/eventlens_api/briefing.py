# ruff: noqa: EM101, TRY003 - fixed composition-detail literals

"""Framework-free briefing use case (T7).

Call order per request (frozen T6 contract, section 4.1)::

    control.admit()
    lease = limiter.try_acquire()
    try:
        ledger = RequestTokenLedger(counter, policy)
        workflow.run(invocation)
    finally:
        lease.release()

The workflow (the compiled briefing graph, injected by composition) calls back
into ``BriefingServices``: ``PrepareSynthesis`` -> ``RetrieveEvidence`` ->
``SynthesizeAnswer``. The caller scope comes from server composition, never
from the request. Typed RAG errors are re-raised unchanged so the HTTP adapter
can map them to stable codes. No FastAPI, SQL, provider SDK, worker or graph
construction lives here.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol
from uuid import UUID, uuid4

from eventlens_contracts.evidence import EvidenceSpan
from eventlens_rag.application.ledger import RequestTokenLedger
from eventlens_rag.application.retrieve import RetrievalOptions, RetrieveEvidence
from eventlens_rag.application.synthesize import PrepareSynthesis, SynthesizeAnswer
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.activation import ActivationDecision
from eventlens_rag.domain.answer import Citation
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.retrieval import (
    QueryControl,
    RetrievalCancelledError,
    RetrievalDeadlineExceededError,
    RetrievalResult,
)
from eventlens_rag.domain.synthesis import SynthesisPlan, SynthesisResult
from eventlens_rag.domain.synthesis_errors import (
    SynthesisAccessDeniedError,
    SynthesisCancelledError,
    SynthesisDeadlineExceededError,
    SynthesisError,
)
from eventlens_rag.domain.synthesis_policy import SynthesisPolicy
from eventlens_rag.ports.access import AccessPolicy
from eventlens_rag.ports.briefing import (
    BriefingInvocation,
    BriefingRoute,
    BriefingWorkflow,
)
from eventlens_rag.ports.source_references import SourceReferenceRegistry
from eventlens_rag.ports.synthesis import ConcurrencyLimiter
from eventlens_rag.ports.tokens import TokenCounter


class BriefingConfigurationError(RuntimeError):
    """Composition wired incompatible services (fixed message)."""

    code: ClassVar[str] = "briefing_configuration_invalid"

    def __init__(self, detail: str) -> None:
        """Name the mismatched wiring, never a secret or content."""
        super().__init__(f"briefing composition invalid: {detail}")


class BriefingWorkflowError(RuntimeError):
    """Workflow ended without a result or typed error (programming error)."""

    code: ClassVar[str] = "briefing_workflow_invalid"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("briefing workflow returned no result")


@dataclass(frozen=True, slots=True)
class BriefingTelemetryEvent:
    """Codes, counts and latency only; never query, prompt or content."""

    invocation_id: UUID
    route: str
    error_code: str | None
    reason: str | None
    model_calls: int
    latency_ms: float


class BriefingTelemetry(Protocol):
    """Telemetry sink for briefing invocations."""

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Record one event."""
        ...


@dataclass(frozen=True, slots=True)
class NullBriefingTelemetry:
    """Drop every event."""

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Ignore ``event``."""
        del event


def _admit(control: QueryControl) -> None:
    try:
        control.admit()
    except RetrievalCancelledError:
        raise SynthesisCancelledError from None
    except RetrievalDeadlineExceededError:
        raise SynthesisDeadlineExceededError from None


@dataclass(frozen=True, slots=True)
class BriefingServices:
    """Graph-facing steps; implements ``eventlens_rag.ports.briefing.BriefingSteps``.

    One ``TokenCounter`` instance is shared by T5 and T6; composition that
    passes different counters or policies is rejected at construction.
    """

    retriever: RetrieveEvidence
    synthesizer: SynthesizeAnswer
    access: AccessPolicy
    tokens: TokenCounter
    policy: SynthesisPolicy

    def __post_init__(self) -> None:
        """Reject wiring that would split token counting or policies."""
        if self.retriever.tokens is not self.tokens:
            raise BriefingConfigurationError("retriever token counter not shared")
        if self.synthesizer.tokens is not self.tokens:
            raise BriefingConfigurationError("synthesizer token counter not shared")
        if self.synthesizer.policy != self.policy:
            raise BriefingConfigurationError("synthesis policy mismatch")

    def prepare(self, invocation: BriefingInvocation) -> SynthesisPlan:
        """Admit, check caller access, plan the budget; no storage/model work."""
        _admit(invocation.control)
        if not self.access.allows(invocation.caller):
            raise SynthesisAccessDeniedError
        return PrepareSynthesis(self.tokens, self.policy).execute(
            invocation.query, invocation.ledger
        )

    def retrieve(
        self, invocation: BriefingInvocation, plan: SynthesisPlan
    ) -> RetrievalResult:
        """T5 retrieval bounded by the planned remaining context budget."""
        return self.retriever.execute(
            invocation.query,
            invocation.caller,
            invocation.control,
            RetrievalOptions(remaining_context_budget=plan.remaining_context_budget),
        )

    def synthesize(
        self, invocation: BriefingInvocation, retrieval: RetrievalResult
    ) -> SynthesisResult:
        """T6 grounded synthesis (or no-call abstention)."""
        return self.synthesizer.execute(
            invocation.query,
            retrieval,
            invocation.caller,
            invocation.control,
            invocation.ledger,
            invocation.lease,
        )


@dataclass(frozen=True, slots=True)
class CitedSource:
    """One public citation: opaque reference plus allowlisted source fields."""

    citation: Citation
    source_ref: str
    source_name: str
    source_domain: str
    source_published_at: datetime | None
    span: EvidenceSpan


@dataclass(frozen=True, slots=True)
class BriefingOutcome:
    """Validated use-case result handed to the HTTP adapter."""

    invocation_id: UUID
    route: BriefingRoute
    result: SynthesisResult
    retrieval: RetrievalResult
    cited_sources: tuple[CitedSource, ...]


def _cited_sources(
    result: SynthesisResult,
    caller: CallerScope,
    references: SourceReferenceRegistry,
) -> tuple[CitedSource, ...]:
    documents = {d.article_id: d for d in result.answer.source_documents}
    cited: list[CitedSource] = []
    for citation in result.answer.citations:
        document = documents.get(citation.article_id)
        span = (
            None
            if document is None
            else next(
                (
                    s
                    for s in document.evidence_spans
                    if s.span_id == citation.evidence_span_id
                ),
                None,
            )
        )
        if document is None or span is None:
            raise BriefingWorkflowError
        cited.append(
            CitedSource(
                citation=citation,
                source_ref=references.issue(caller, span),
                source_name=document.source_name,
                source_domain=document.source_domain,
                source_published_at=document.source_published_at,
                span=span,
            )
        )
    return tuple(cited)


@dataclass(frozen=True, slots=True)
class BriefingUseCase:
    """Admission, per-request isolation and result shaping around the workflow."""

    workflow: BriefingWorkflow
    services: BriefingServices
    limiter: ConcurrencyLimiter
    activation: ActivationDecision
    caller: CallerScope
    references: SourceReferenceRegistry
    telemetry: BriefingTelemetry = NullBriefingTelemetry()
    monotonic: Callable[[], float] = time.monotonic
    ids: Callable[[], UUID] = uuid4

    def __post_init__(self) -> None:
        """Reject a limiter or activation that disagrees with the policy."""
        policy = self.services.policy
        if self.limiter.capacity != policy.concurrency.max_concurrent_requests:
            raise BriefingConfigurationError("limiter capacity differs from policy")
        if self.services.synthesizer.activation != self.activation:
            raise BriefingConfigurationError("activation decision mismatch")

    def new_control(self) -> QueryControl:
        """Fresh end-to-end deadline/cancellation for one request."""
        return QueryControl(
            self.services.policy.deadlines.request_deadline_seconds, self.monotonic
        )

    def execute(
        self,
        query: BriefingQuery,
        *,
        caller: CallerScope | None = None,
        control: QueryControl | None = None,
    ) -> BriefingOutcome:
        """Run one isolated briefing; typed failures propagate unchanged.

        ``caller`` comes from the server-side caller resolver; it defaults to
        the composed caller and is never taken from the request body.
        """
        caller = caller if caller is not None else self.caller
        control = control if control is not None else self.new_control()
        started = self.monotonic()
        invocation_id = self.ids()
        try:
            _admit(control)  # cancelled/expired: no lease, no DB/model work
            lease = self.limiter.try_acquire()
        except SynthesisError as error:
            self._emit(invocation_id, "rejected", None, error, started)
            raise
        try:
            invocation = BriefingInvocation(
                invocation_id=invocation_id,
                query=query,
                caller=caller,
                control=control,
                ledger=RequestTokenLedger(self.services.tokens, self.services.policy),
                lease=lease,
            )
            run = self.workflow.run(invocation)
        finally:
            lease.release()
        self._emit(invocation_id, run.route.value, run.result, run.error, started)
        if run.error is not None:
            raise run.error
        if run.result is None or run.retrieval is None:
            raise BriefingWorkflowError
        return BriefingOutcome(
            invocation_id=invocation_id,
            route=run.route,
            result=run.result,
            retrieval=run.retrieval,
            cited_sources=_cited_sources(run.result, caller, self.references),
        )

    def _emit(
        self,
        invocation_id: UUID,
        route: str,
        result: SynthesisResult | None,
        error: Exception | None,
        started: float,
    ) -> None:
        code: object = getattr(error, "code", None)
        reason: object = getattr(error, "reason", None)
        self.telemetry.emit(
            BriefingTelemetryEvent(
                invocation_id=invocation_id,
                route=route,
                error_code=None if code is None else str(code),
                reason=None if reason is None else str(reason),
                model_calls=0 if result is None else result.usage.model_calls,
                latency_ms=(self.monotonic() - started) * 1000,
            )
        )
