"""T6 use-case DTOs consumed by T7 (frozen interface, phase 1).

Successful and abstaining runs return ``SynthesisResult``; every failure is a
typed ``SynthesisError`` (see ``synthesis_errors``). ``SynthesisResult.answer``
is a fully validated ``BriefingAnswer``; its ``source_documents`` are internal
trusted documents for validation and must not be dumped as a public API body
(T7 maps a public DTO). Nothing here carries prompts, raw provider bodies,
credentials, storage URIs or canonical URLs from the model.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from backend.rag.domain.answer import BriefingAnswer, Citation, Claim, Uncertainty
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.retrieval import (
    FilterCoverage,
    RetrievedArticle,
    RetrievedSpan,
    StageMetric,
)
from backend.rag.domain.tokenizer import TokenizerIdentity
from backend.shared.contracts.evidence import EvidenceSpan


class SynthesisOutcome(StrEnum):
    """Non-error outcomes; failures are typed exceptions."""

    ANSWERED = "answered"
    ABSTAINED_NO_EVIDENCE = "abstained_no_evidence"
    ABSTAINED_BY_MODEL = "abstained_by_model"


class UsageStatus(StrEnum):
    """Provider-reported usage state for the (single) call."""

    NOT_CALLED = "not_called"
    REPORTED = "reported"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Validated non-negative provider-reported token usage."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ChatModelIdentity:
    """Chat model identity recorded in provenance."""

    model_id: str
    model_version: str


@dataclass(frozen=True, slots=True)
class LedgerReservation:
    """One atomic admission: counted input, reserved output, reserved cost."""

    reservation_id: int
    input_tokens: int
    output_tokens: int
    cost: int


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Cumulative request totals (charged = settled, open = reserved, unsettled)."""

    tokenizer: TokenizerIdentity
    admissions: int
    input_charged: int
    output_charged: int
    cost_charged: int
    input_open: int
    output_open: int
    cost_open: int
    remaining_input: int
    remaining_output: int
    remaining_cost: int
    input_drift_tokens: int | None


@dataclass(frozen=True, slots=True)
class SynthesisPlan:
    """Preparation result: what T5 may spend on evidence for this request.

    T7/smoke pass ``RetrievalOptions(remaining_context_budget=
    plan.remaining_context_budget)`` to T5 with the same ``TokenCounter``.
    """

    remaining_context_budget: int
    header_tokens: int
    framing_reserve_tokens: int
    output_reservation: int
    tokenizer: TokenizerIdentity
    synthesis_policy_id: str


@dataclass(frozen=True, slots=True)
class SelectedArticle:
    """An article and the subset of its retrieved spans chosen to send."""

    article: RetrievedArticle
    spans: tuple[RetrievedSpan, ...]


@dataclass(frozen=True, slots=True)
class TrustedSource:
    """Current document (selected spans attached via validation) + sent spans."""

    document: ArticleDocument
    sent_spans: tuple[EvidenceSpan, ...]


@dataclass(frozen=True, slots=True)
class TrustedSourceRegistry:
    """Server-side registry: only spans actually sent may be cited."""

    sources: Mapping[UUID, TrustedSource]
    span_owner: Mapping[UUID, UUID]

    def lookup(self, span_id: UUID) -> tuple[TrustedSource, EvidenceSpan] | None:
        """Return the owning source and sent span, or ``None`` if not sent."""
        article_id = self.span_owner.get(span_id)
        if article_id is None:
            return None
        source = self.sources[article_id]
        for span in source.sent_spans:
            if span.span_id == span_id:
                return source, span
        return None


@dataclass(frozen=True, slots=True)
class GroundedDraft:
    """Mechanically validated claims/citations/uncertainties (pre-render)."""

    claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    uncertainties: tuple[Uncertainty, ...]
    evidence_status: Literal["sufficient", "insufficient"]


@dataclass(frozen=True, slots=True)
class SynthesisUsage:
    """Request-level token/cost accounting (fixture cost unit)."""

    model_calls: int
    input_tokens_counted: int
    input_tokens_charged: int
    output_tokens_reserved: int
    output_tokens_charged: int
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    usage_status: UsageStatus
    input_drift_tokens: int | None
    cost_charged: int
    cost_unit: str


@dataclass(frozen=True, slots=True)
class SynthesisProvenance:
    """Versions that make a synthesis reproducible; set by the server only."""

    synthesis_policy_id: str
    activation_policy_id: str
    mode: Literal["fixture", "live"]
    chat_model: ChatModelIdentity
    tokenizer: TokenizerIdentity
    prompt_version: str
    draft_schema_version: str
    template_version: str
    serialization: str
    retrieval_policy_id: str
    index_version: str
    corpus_generation: str


@dataclass(frozen=True, slots=True)
class SynthesisMetrics:
    """Counts and latencies only; never text, prompts or URIs."""

    stages: tuple[StageMetric, ...]
    total_latency_ms: float
    articles_sent: int
    spans_sent: int
    claims: int
    citations: int
    source_batch_reads: int


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Validated answer or explicit abstention for T7 to map to HTTP."""

    outcome: SynthesisOutcome
    answer: BriefingAnswer
    abstention_reason: str | None
    coverage: FilterCoverage
    usage: SynthesisUsage
    provenance: SynthesisProvenance
    metrics: SynthesisMetrics


@dataclass(frozen=True, slots=True)
class SynthesisTelemetryEvent:
    """One per execute: outcome or error code, never content."""

    outcome: str
    reason: str | None
    usage: SynthesisUsage | None
    provenance: SynthesisProvenance | None
    metrics: SynthesisMetrics | None
