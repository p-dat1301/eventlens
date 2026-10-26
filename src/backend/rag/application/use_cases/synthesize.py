"""Grounded synthesis use case (T6): preparation, one budgeted call, grounding.

``PrepareSynthesis`` runs before T5 and computes the evidence allowance left
after the full serialized header (instructions, question, framing, schema,
wrappers) and a framing reserve. ``SynthesizeAnswer`` takes a typed T5
``RetrievalResult`` and either returns a validated ``SynthesisResult``
(answer or explicit abstention) or raises a typed ``SynthesisError``.

Current-evidence boundary: one batched ``current_articles`` read before the
provider call and one after it; access is checked at entry, after each read
and at the return boundary of every path (including abstentions). No storage
transaction is held across the call; a change committed after the final
check and access decision is outside this guarantee (T7/T12/T13 own the
response-emission boundary).
"""

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Thread
from uuid import UUID, uuid4

from backend.rag.application.citations import (
    build_registry,
    select_evidence,
    validate_draft,
)
from backend.rag.application.ledger import valid_usage
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.synthesis import (
    AdmissionLease,
    CurrentArticleReader,
    SynthesisModelPort,
    SynthesisRequest,
    SynthesisResponse,
    SynthesisTelemetry,
    TokenLedger,
)
from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.application.rendering import (
    abstention_answer,
    render_answer,
    template_uncertainty,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.activation import ActivationDecision
from backend.rag.domain.answer import BriefingAnswer, Uncertainty
from backend.rag.domain.model_input import (
    SERIALIZATION,
    SYNTHESIS_INSTRUCTIONS,
    SYNTHESIS_PROMPT_VERSION,
    ModelInput,
    SerializedModelInput,
    serialize_model_input,
    source_payload,
)
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    CoverageStatus,
    EmptyReason,
    QueryControl,
    ResultStatus,
    RetrievalCancelledError,
    RetrievalDeadlineExceededError,
    RetrievalResult,
    RetrievalStorageError,
    StageMetric,
)
from backend.rag.domain.synthesis import (
    LedgerReservation,
    SelectedArticle,
    SynthesisMetrics,
    SynthesisOutcome,
    SynthesisPlan,
    SynthesisProvenance,
    SynthesisResult,
    SynthesisTelemetryEvent,
    SynthesisUsage,
    TrustedSourceRegistry,
    UsageStatus,
)
from backend.rag.domain.synthesis_draft import DRAFT_SCHEMA_VERSION, parse_draft
from backend.rag.domain.synthesis_errors import (
    BudgetFailureReason,
    ProviderFailureCode,
    SynthesisAccessDeniedError,
    SynthesisBudgetError,
    SynthesisCancelledError,
    SynthesisConfigurationError,
    SynthesisDeadlineExceededError,
    SynthesisError,
    SynthesisProviderError,
    SynthesisStorageError,
)
from backend.rag.domain.synthesis_policy import ServerReasonCode, SynthesisPolicy

_NO_EVIDENCE: dict[EmptyReason, ServerReasonCode] = {
    EmptyReason.NO_MATCHES: "no_matches",
    EmptyReason.NO_QUERY_TERMS: "no_query_terms",
    EmptyReason.EMPTY_PUBLICATION_RANGE: "empty_publication_range",
    EmptyReason.FILTER_UNAVAILABLE: "filter_unavailable",
    EmptyReason.DROPPED_AT_FINAL_CHECK: "dropped_at_final_check",
}
_LIMITED_COVERAGE = frozenset(
    {CoverageStatus.PARTIAL, CoverageStatus.FILTER_UNAVAILABLE}
)


def _admit(control: QueryControl) -> None:
    """Shared admission; retrieval control errors become synthesis errors."""
    try:
        control.admit()
    except RetrievalCancelledError:
        raise SynthesisCancelledError from None
    except RetrievalDeadlineExceededError:
        raise SynthesisDeadlineExceededError from None


def _header(policy: SynthesisPolicy, query: BriefingQuery) -> SerializedModelInput:
    return serialize_model_input(
        ModelInput(
            prompt_version=policy.prompt_version,
            instructions=SYNTHESIS_INSTRUCTIONS,
            question=query.query,
            sources=(),
        )
    )


def _check_policy(policy: SynthesisPolicy) -> None:
    if (
        policy.prompt_version != SYNTHESIS_PROMPT_VERSION
        or policy.counting.serialization != SERIALIZATION
        or policy.draft_schema_version != DRAFT_SCHEMA_VERSION
    ):
        message = "synthesis policy does not match the frozen serialization"
        raise SynthesisConfigurationError(message)


@dataclass(frozen=True, slots=True)
class PrepareSynthesis:
    """Budget helper run before T5 with the same ``TokenCounter`` (no I/O)."""

    tokens: TokenCounter
    policy: SynthesisPolicy

    def execute(self, query: BriefingQuery, ledger: TokenLedger) -> SynthesisPlan:
        """Return the evidence allowance for T5 or raise a zero-call budget error.

        Also refuses early when the request has no output or cost room left
        for one call, since retrieval work would be wasted.
        """
        _check_policy(self.policy)
        if ledger.identity != self.tokens.identity:
            message = "ledger and retrieval must share one tokenizer"
            raise SynthesisConfigurationError(message)
        budget = self.policy.budget
        header_tokens = ledger.count(_header(self.policy, query))
        snapshot = ledger.snapshot()
        if header_tokens > snapshot.remaining_input:
            raise SynthesisBudgetError(BudgetFailureReason.HEADER_EXCEEDS_INPUT)
        remaining = min(
            query.context_budget,
            snapshot.remaining_input - header_tokens - budget.framing_reserve_tokens,
        )
        if remaining < budget.min_evidence_tokens:
            raise SynthesisBudgetError(BudgetFailureReason.CONTEXT_TOO_SMALL)
        reservation = budget.output_reservation_per_call
        if reservation > snapshot.remaining_output:
            raise SynthesisBudgetError(BudgetFailureReason.OUTPUT_CAP)
        pricing = self.policy.pricing
        floor = (
            (header_tokens + budget.min_evidence_tokens) * pricing.input_per_token
            + reservation * pricing.output_per_token
            + pricing.per_call_fee
        )
        if floor > snapshot.remaining_cost:
            raise SynthesisBudgetError(BudgetFailureReason.COST_CAP)
        return SynthesisPlan(
            remaining_context_budget=remaining,
            header_tokens=header_tokens,
            framing_reserve_tokens=budget.framing_reserve_tokens,
            output_reservation=reservation,
            tokenizer=ledger.identity,
            synthesis_policy_id=self.policy.policy_id,
        )


@dataclass(slots=True)
class _CallSlot:
    response: SynthesisResponse | None = None
    error: SynthesisError | None = None
    done: Event = field(default_factory=Event)


@dataclass(slots=True)
class _Run:
    """Per-execute bookkeeping (counts and timings only, never content)."""

    timer: Callable[[], float]
    started: float
    stages: list[StageMetric] = field(default_factory=list[StageMetric])
    reads: int = 0
    calls: int = 0
    counted: int = 0
    reservation: LedgerReservation | None = None
    usage: SynthesisResponse | None = None
    usage_status: UsageStatus = UsageStatus.NOT_CALLED
    articles_sent: int = 0
    spans_sent: int = 0

    @contextmanager
    def stage(self, name: str, count_in: int) -> Generator[list[int], None, None]:
        out = [count_in]
        begin = self.timer()
        yield out
        self.stages.append(
            StageMetric(
                stage=name,
                latency_ms=(self.timer() - begin) * 1000,
                candidates_in=count_in,
                candidates_out=out[0],
            )
        )


@dataclass(frozen=True, slots=True)
class SynthesizeAnswer:
    """Application service; every dependency is an injected port."""

    model: SynthesisModelPort
    sources: CurrentArticleReader
    access: AccessPolicy
    tokens: TokenCounter
    policy: SynthesisPolicy
    activation: ActivationDecision
    telemetry: SynthesisTelemetry | None = None
    ids: Callable[[], UUID] = uuid4
    timer: Callable[[], float] = time.perf_counter

    def execute(  # noqa: PLR0913, PLR0917
        self,
        query: BriefingQuery,
        retrieval: RetrievalResult,
        caller: CallerScope,
        control: QueryControl,
        ledger: TokenLedger,
        lease: AdmissionLease,
    ) -> SynthesisResult:
        """Validated answer/abstention or a typed ``SynthesisError``."""
        run = _Run(self.timer, self.timer())
        try:
            result = self._execute(
                query, retrieval, caller, control, ledger, lease, run
            )
        except SynthesisError as error:
            reason: object = getattr(error, "reason", None)
            self._emit(
                SynthesisTelemetryEvent(
                    outcome=error.code,
                    reason=None if reason is None else str(reason),
                    usage=None,
                    provenance=None,
                    metrics=None,
                )
            )
            raise
        self._emit(
            SynthesisTelemetryEvent(
                outcome=result.outcome.value,
                reason=result.abstention_reason,
                usage=result.usage,
                provenance=result.provenance,
                metrics=result.metrics,
            )
        )
        return result

    def _emit(self, event: SynthesisTelemetryEvent) -> None:
        if self.telemetry is not None:
            self.telemetry.emit(event)

    def _authorize(self, caller: CallerScope) -> None:
        if not self.access.allows(caller):
            raise SynthesisAccessDeniedError

    def _check_wiring(self, ledger: TokenLedger, lease: AdmissionLease) -> None:
        _check_policy(self.policy)
        binding = self.activation.chat_model
        identity = self.model.identity
        if (identity.model_id, identity.model_version) != (
            binding.model_id,
            binding.model_version,
        ):
            message = "chat model differs from the activation decision"
            raise SynthesisConfigurationError(message)
        tokenizer = ledger.identity
        if tokenizer != self.tokens.identity or (
            tokenizer.tokenizer_id,
            tokenizer.version,
        ) != (binding.tokenizer_id, binding.tokenizer_version):
            message = "tokenizer differs from the activation decision"
            raise SynthesisConfigurationError(message)
        if lease.released:
            message = "admission lease already released"
            raise SynthesisConfigurationError(message)

    def _execute(  # noqa: PLR0913, PLR0917
        self,
        query: BriefingQuery,
        retrieval: RetrievalResult,
        caller: CallerScope,
        control: QueryControl,
        ledger: TokenLedger,
        lease: AdmissionLease,
        run: _Run,
    ) -> SynthesisResult:
        _admit(control)
        self._check_wiring(ledger, lease)
        self._authorize(caller)
        if retrieval.status is ResultStatus.EMPTY:
            return self._empty(retrieval, caller, control, ledger, run)
        if not retrieval.articles:
            message = "evidence result without articles"
            raise SynthesisConfigurationError(message)
        context = retrieval.context
        if (context.tokenizer_id, context.tokenizer_version) != (
            ledger.identity.tokenizer_id,
            ledger.identity.version,
        ):
            # T5 and T6 must count with one TokenCounter/mapping (D32).
            message = "retrieval context was counted with a different tokenizer"
            raise SynthesisConfigurationError(message)
        limit = min(self.policy.budget.max_articles, query.result_limit)
        selected = select_evidence(retrieval, limit)
        with run.stage("precheck", len(selected)) as out:
            _ = self._read(
                selected, frozenset(_span_ids(selected)), query, caller, control, run
            )
            out[0] = len(selected)
        serialized = self._fit(query, selected, ledger)
        sent = serialized.sent_span_ids
        run.articles_sent = sum(
            1
            for entry in selected
            if any(item.span.span_id in sent for item in entry.spans)
        )
        run.spans_sent = len(serialized.sent_span_ids)
        response = self._call(serialized, control, ledger, lease, run)
        with run.stage("parse", 1):
            draft = parse_draft(response.content)
        with run.stage("postcheck", run.articles_sent) as out:
            registry = self._read(
                selected, serialized.sent_span_ids, query, caller, control, run
            )
            out[0] = len(registry.sources)
        with run.stage("ground", len(draft.claims)) as out:
            grounded = validate_draft(draft, registry, self.policy, self.ids)
            server = self._coverage_uncertainties(retrieval)
            answer = render_answer(
                grounded,
                registry,
                server_uncertainties=server,
                retrieved_at=retrieval.provenance.query_time,
                model_version=self.model.identity.model_version,
                index_version=retrieval.provenance.index_version,
            )
            out[0] = len(answer.claims)
        if grounded.evidence_status == "insufficient":
            outcome = SynthesisOutcome.ABSTAINED_BY_MODEL
            reason: str | None = "insufficient_evidence"
        else:
            outcome = SynthesisOutcome.ANSWERED
            reason = None
        return self._finish(
            outcome, reason, answer, retrieval, caller, control, ledger, run
        )

    def _coverage_uncertainties(
        self, retrieval: RetrievalResult
    ) -> tuple[Uncertainty, ...]:
        if retrieval.coverage.status in _LIMITED_COVERAGE:
            return (
                template_uncertainty("partial_filter_coverage", self.policy, self.ids),
            )
        return ()

    def _empty(
        self,
        retrieval: RetrievalResult,
        caller: CallerScope,
        control: QueryControl,
        ledger: TokenLedger,
        run: _Run,
    ) -> SynthesisResult:
        empty = retrieval.empty_reason
        if empty is EmptyReason.CONTEXT_BUDGET_TOO_SMALL:
            raise SynthesisBudgetError(BudgetFailureReason.CONTEXT_TOO_SMALL)
        if empty is None or empty not in _NO_EVIDENCE:
            message = "empty retrieval result without a known reason"
            raise SynthesisConfigurationError(message)
        code = _NO_EVIDENCE[empty]
        answer = abstention_answer(
            code,
            self.policy,
            ids=self.ids,
            retrieved_at=retrieval.provenance.query_time,
            model_version=self.activation.chat_model.model_version,
            index_version=retrieval.provenance.index_version,
        )
        return self._finish(
            SynthesisOutcome.ABSTAINED_NO_EVIDENCE,
            code,
            answer,
            retrieval,
            caller,
            control,
            ledger,
            run,
        )

    def _read(  # noqa: PLR0913, PLR0917
        self,
        selected: tuple[SelectedArticle, ...],
        span_ids: frozenset[UUID],
        query: BriefingQuery,
        caller: CallerScope,
        control: QueryControl,
        run: _Run,
    ) -> TrustedSourceRegistry:
        """One batched current read, re-authorized after it completes."""
        article_ids = tuple(
            entry.article.article_id
            for entry in selected
            if any(item.span.span_id in span_ids for item in entry.spans)
        )
        _admit(control)
        run.reads += 1
        try:
            current = self.sources.current_articles(article_ids)
        except RetrievalStorageError:
            raise SynthesisStorageError from None
        _admit(control)
        # A revocation committed while the read was in flight must still win.
        self._authorize(caller)
        return build_registry(selected, span_ids, current, query)

    def _fit(
        self,
        query: BriefingQuery,
        selected: tuple[SelectedArticle, ...],
        ledger: TokenLedger,
    ) -> SerializedModelInput:
        """Recount the full payload; drop whole trailing spans until it fits."""
        groups = [list(entry.spans) for entry in selected]
        remaining = ledger.snapshot().remaining_input
        while any(groups):
            serialized = serialize_model_input(
                ModelInput(
                    prompt_version=self.policy.prompt_version,
                    instructions=SYNTHESIS_INSTRUCTIONS,
                    question=query.query,
                    sources=tuple(
                        source_payload(entry.article, tuple(spans))
                        for entry, spans in zip(selected, groups, strict=True)
                        if spans
                    ),
                )
            )
            if ledger.count(serialized) <= remaining:
                return serialized
            last = max(index for index, spans in enumerate(groups) if spans)
            _ = groups[last].pop()
        raise SynthesisBudgetError(BudgetFailureReason.EVIDENCE_EXCEEDS_BUDGET)

    def _call(
        self,
        serialized: SerializedModelInput,
        control: QueryControl,
        ledger: TokenLedger,
        lease: AdmissionLease,
        run: _Run,
    ) -> SynthesisResponse:
        """At most one pinned, reserved, time-sliced provider call."""
        budget = self.policy.budget
        if ledger.snapshot().admissions >= budget.max_model_calls_per_request:
            raise SynthesisBudgetError(BudgetFailureReason.CALL_CAP)
        _admit(control)
        run.counted = ledger.count(serialized)
        reservation = ledger.reserve(serialized, budget.output_reservation_per_call)
        run.reservation = reservation
        _admit(control)
        seconds = min(self.policy.deadlines.call_timeout_seconds, control.remaining())
        request = SynthesisRequest(
            serialized=serialized,
            max_output_tokens=reservation.output_tokens,
            timeout_seconds=seconds,
        )
        with run.stage("model_call", 1):
            slot = self._run_pinned(request, control, lease)
            run.calls += 1
        if not slot.done.is_set() or slot.response is None:
            ledger.settle(reservation, "failure", None)
            _admit(control)
            if slot.error is not None:
                raise slot.error
            raise SynthesisProviderError(ProviderFailureCode.TIMEOUT)
        response = slot.response
        usage = valid_usage(response.usage)
        status = response.usage_status
        if status is UsageStatus.REPORTED and usage is None:
            status = UsageStatus.INVALID
        ledger.settle(
            reservation, "success", usage if status is UsageStatus.REPORTED else None
        )
        run.usage = response
        run.usage_status = status
        _admit(control)
        identity = self.model.identity
        if response.reported_model is not None and response.reported_model not in {
            identity.model_id,
            identity.model_version,
        }:
            raise SynthesisProviderError(ProviderFailureCode.MODEL_MISMATCH)
        return response

    def _run_pinned(
        self, request: SynthesisRequest, control: QueryControl, lease: AdmissionLease
    ) -> _CallSlot:
        """Run the call in a thread holding a lease pin until it returns."""
        slot = _CallSlot()
        wake = Event()
        unpin = lease.pin()

        def target() -> None:
            try:
                slot.response = self.model.synthesize(request)
            except SynthesisError as error:
                # Typed adapter errors (provider, configuration, activation)
                # keep their code; the reservation is settled as a failure.
                slot.error = error
            except Exception:  # noqa: BLE001 - adapter contract breach; no raw text
                slot.error = SynthesisProviderError(ProviderFailureCode.UNAVAILABLE)
            finally:
                slot.done.set()
                unpin()
                wake.set()

        control.on_cancel(wake.set)
        Thread(target=target, name="eventlens-synthesis", daemon=True).start()
        _ = wake.wait(timeout=max(request.timeout_seconds, 0.0))
        return slot

    def _finish(  # noqa: PLR0913, PLR0917
        self,
        outcome: SynthesisOutcome,
        reason: str | None,
        answer: BriefingAnswer,
        retrieval: RetrievalResult,
        caller: CallerScope,
        control: QueryControl,
        ledger: TokenLedger,
        run: _Run,
    ) -> SynthesisResult:
        """Return boundary: admission and access are re-checked on every path."""
        _admit(control)
        self._authorize(caller)
        snapshot = ledger.snapshot()
        provider = None if run.usage is None else valid_usage(run.usage.usage)
        reported = run.usage_status is UsageStatus.REPORTED and provider is not None
        usage = SynthesisUsage(
            model_calls=run.calls,
            input_tokens_counted=run.counted,
            input_tokens_charged=snapshot.input_charged,
            output_tokens_reserved=(
                0 if run.reservation is None else run.reservation.output_tokens
            ),
            output_tokens_charged=snapshot.output_charged,
            provider_input_tokens=provider.input_tokens
            if reported and provider
            else None,
            provider_output_tokens=(
                provider.output_tokens if reported and provider else None
            ),
            usage_status=run.usage_status,
            input_drift_tokens=snapshot.input_drift_tokens,
            cost_charged=snapshot.cost_charged,
            cost_unit=self.policy.pricing.unit,
        )
        provenance = SynthesisProvenance(
            synthesis_policy_id=self.policy.policy_id,
            activation_policy_id=self.activation.policy_id,
            mode=self.activation.mode,
            chat_model=self.model.identity,
            tokenizer=ledger.identity,
            prompt_version=self.policy.prompt_version,
            draft_schema_version=self.policy.draft_schema_version,
            template_version=self.policy.template_version,
            serialization=SERIALIZATION,
            retrieval_policy_id=retrieval.provenance.policy_id,
            index_version=retrieval.provenance.index_version,
            corpus_generation=retrieval.provenance.corpus_generation,
        )
        metrics = SynthesisMetrics(
            stages=tuple(run.stages),
            total_latency_ms=(self.timer() - run.started) * 1000,
            articles_sent=run.articles_sent,
            spans_sent=run.spans_sent,
            claims=len(answer.claims),
            citations=len(answer.citations),
            source_batch_reads=run.reads,
        )
        return SynthesisResult(
            outcome=outcome,
            answer=answer,
            abstention_reason=reason,
            coverage=retrieval.coverage,
            usage=usage,
            provenance=provenance,
            metrics=metrics,
        )


def _span_ids(selected: tuple[SelectedArticle, ...]) -> list[UUID]:
    return [item.span.span_id for entry in selected for item in entry.spans]


__all__ = ["PrepareSynthesis", "SynthesizeAnswer"]
