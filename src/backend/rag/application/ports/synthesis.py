"""T6 synthesis ports (framework-free, frozen interface).

Adapters implement these protocols; the application never imports adapters.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from backend.rag.application.ports.retrieval import CurrentArticleState
from backend.rag.domain.model_input import SerializedModelInput
from backend.rag.domain.synthesis import (
    ChatModelIdentity,
    LedgerReservation,
    LedgerSnapshot,
    ProviderUsage,
    SynthesisTelemetryEvent,
    UsageStatus,
)
from backend.rag.domain.tokenizer import TokenizerIdentity


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """One provider call: exact serialized input, output cap and time slice."""

    serialized: SerializedModelInput
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SynthesisResponse:
    """Untrusted provider content; parsed strictly by the application."""

    content: str
    usage: ProviderUsage | None
    usage_status: UsageStatus
    reported_model: str | None


class SynthesisModelPort(Protocol):
    """Exactly one call per ``synthesize``; no internal retry or repair.

    Raises only ``SynthesisProviderError`` (stable ``ProviderFailureCode``).
    """

    @property
    def identity(self) -> ChatModelIdentity:
        """Configured chat model identity."""
        ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Return the structured content or raise a typed provider error."""
        ...


class TokenLedger(Protocol):
    """Request-scoped cumulative ledger reusing ``TokenCounter`` (D32).

    ``reserve`` counts ``serialized.counted_text`` itself, then checks input,
    output and cost caps atomically; on refusal it raises
    ``SynthesisBudgetError`` and records nothing. ``settle`` applies the
    policy settlement rules exactly once per reservation and never resets.
    """

    @property
    def identity(self) -> TokenizerIdentity:
        """Tokenizer identity used for every count."""
        ...

    def count(self, serialized: SerializedModelInput) -> int:
        """Tokens of the full serialized input (no admission)."""
        ...

    def reserve(
        self, serialized: SerializedModelInput, output_tokens: int
    ) -> LedgerReservation:
        """Atomically admit one call or raise ``SynthesisBudgetError``."""
        ...

    def settle(
        self,
        reservation: LedgerReservation,
        outcome: Literal["success", "failure"],
        usage: ProviderUsage | None,
    ) -> None:
        """Charge the reservation per policy (idempotence is an error)."""
        ...

    def snapshot(self) -> LedgerSnapshot:
        """Current cumulative totals."""
        ...


class AdmissionLease(Protocol):
    """One admitted request slot.

    The slot is freed only when the owner has called ``release`` and every
    ``pin`` has been undone, so an in-flight provider call that outlives a
    cancelled/timed-out request keeps occupying capacity.
    """

    @property
    def released(self) -> bool:
        """Whether the owner released the lease."""
        ...

    def pin(self) -> Callable[[], None]:
        """Hold the slot for in-flight work; the returned unpin is idempotent."""
        ...

    def release(self) -> None:
        """Owner release (idempotent)."""
        ...


class ConcurrencyLimiter(Protocol):
    """Process-local shared limiter; one instance per runtime/composition."""

    @property
    def capacity(self) -> int:
        """Maximum concurrent admitted requests."""
        ...

    @property
    def in_use(self) -> int:
        """Slots currently held (including pinned in-flight calls)."""
        ...

    def try_acquire(self) -> AdmissionLease:
        """Admit or raise ``SynthesisConcurrencyRejectedError``; never queue."""
        ...


class CurrentArticleReader(Protocol):
    """Batched current-revision read; ``RetrievalStore`` satisfies it."""

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        """One batched final check of current revisions and valid metadata."""
        ...


class SynthesisTelemetry(Protocol):
    """Receive one content-free event per execute."""

    def emit(self, event: SynthesisTelemetryEvent) -> None:
        """Record one event."""
        ...


class TrustedIssuerRegistry(Protocol):
    """Composition-owned trust root for live activation (none ships in T6)."""

    def approves(self, issuer: str, policy_id: str, policy_sha256: str) -> bool:
        """Whether this exact policy artifact is approved by a trusted issuer."""
        ...
