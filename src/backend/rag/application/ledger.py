"""Request-scoped cumulative token/cost ledger (D32).

One ``RequestTokenLedger`` belongs to one user request and is shared by every
admission of that request (preparation, synthesis calls, future retries). It
reuses the injected ``TokenCounter`` on the exact serialized input, so what is
counted is what is sent. ``reserve`` admits atomically under one lock; settled
charges never decrease, so a failure cannot refund allowance into an
unbounded retry loop. The ledger has no call cap: the service enforces
``max_model_calls_per_request``.
"""

from threading import Lock
from typing import Literal

from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.domain.model_input import SerializedModelInput
from backend.rag.domain.synthesis import (
    LedgerReservation,
    LedgerSnapshot,
    ProviderUsage,
)
from backend.rag.domain.synthesis_errors import (
    BudgetFailureReason,
    SynthesisBudgetError,
    SynthesisConfigurationError,
)
from backend.rag.domain.synthesis_policy import SynthesisPolicy
from backend.rag.domain.tokenizer import TokenizerIdentity


def valid_usage(usage: ProviderUsage | None) -> ProviderUsage | None:
    """Return ``usage`` only when both counts are non-negative plain ints."""
    if usage is None:
        return None
    counts = (usage.input_tokens, usage.output_tokens)
    if all(type(value) is int and value >= 0 for value in counts):
        return usage
    return None


class RequestTokenLedger:
    """Thread-safe cumulative ledger implementing the ``TokenLedger`` port."""

    def __init__(self, counter: TokenCounter, policy: SynthesisPolicy) -> None:
        """Bind the shared counter and the frozen policy caps/rates."""
        self._counter: TokenCounter = counter
        self._policy: SynthesisPolicy = policy
        self._lock: Lock = Lock()
        self._next_id: int = 0
        self._admissions: int = 0
        self._input_charged: int = 0
        self._output_charged: int = 0
        self._cost_charged: int = 0
        self._open: dict[int, LedgerReservation] = {}
        self._drift: int | None = None

    @property
    def identity(self) -> TokenizerIdentity:
        """Tokenizer identity used for every count."""
        return self._counter.identity

    def count(self, serialized: SerializedModelInput) -> int:
        """Tokens of the full serialized input (no admission)."""
        return self._counter.count(serialized.counted_text)

    def _cost(self, input_tokens: int, output_tokens: int) -> int:
        pricing = self._policy.pricing
        return (
            input_tokens * pricing.input_per_token
            + output_tokens * pricing.output_per_token
            + pricing.per_call_fee
        )

    def reserve(
        self, serialized: SerializedModelInput, output_tokens: int
    ) -> LedgerReservation:
        """Atomically admit one call or raise ``SynthesisBudgetError``."""
        if type(output_tokens) is not int or output_tokens < 0:
            message = "output reservation must be a non-negative int"
            raise SynthesisConfigurationError(message)
        tokens = self.count(serialized)
        cost = self._cost(tokens, output_tokens)
        budget = self._policy.budget
        with self._lock:
            input_open = sum(item.input_tokens for item in self._open.values())
            output_open = sum(item.output_tokens for item in self._open.values())
            cost_open = sum(item.cost for item in self._open.values())
            if (
                self._input_charged + input_open + tokens
                > budget.max_input_tokens_per_request
            ):
                raise SynthesisBudgetError(BudgetFailureReason.INPUT_CAP)
            if (
                self._output_charged + output_open + output_tokens
                > budget.max_output_tokens_per_request
            ):
                raise SynthesisBudgetError(BudgetFailureReason.OUTPUT_CAP)
            if (
                self._cost_charged + cost_open + cost
                > self._policy.pricing.request_cost_cap
            ):
                raise SynthesisBudgetError(BudgetFailureReason.COST_CAP)
            self._next_id += 1
            reservation = LedgerReservation(
                reservation_id=self._next_id,
                input_tokens=tokens,
                output_tokens=output_tokens,
                cost=cost,
            )
            self._open[reservation.reservation_id] = reservation
            self._admissions += 1
            return reservation

    def settle(
        self,
        reservation: LedgerReservation,
        outcome: Literal["success", "failure"],
        usage: ProviderUsage | None,
    ) -> None:
        """Charge per policy exactly once; never refund below the charges."""
        with self._lock:
            held = self._open.get(reservation.reservation_id)
            if held is None or held != reservation:
                message = "reservation is unknown or already settled"
                raise SynthesisConfigurationError(message)
            del self._open[reservation.reservation_id]
            reported = valid_usage(usage) if outcome == "success" else None
            if reported is None:
                input_tokens = held.input_tokens
                output_tokens = held.output_tokens
                cost = held.cost
            else:
                input_tokens = max(held.input_tokens, reported.input_tokens)
                output_tokens = reported.output_tokens
                cost = self._cost(input_tokens, output_tokens)
                drift = reported.input_tokens - held.input_tokens
                self._drift = drift if self._drift is None else self._drift + drift
            self._input_charged += input_tokens
            self._output_charged += output_tokens
            self._cost_charged += cost

    def snapshot(self) -> LedgerSnapshot:
        """Current cumulative totals (remaining values never go negative)."""
        budget = self._policy.budget
        with self._lock:
            input_open = sum(item.input_tokens for item in self._open.values())
            output_open = sum(item.output_tokens for item in self._open.values())
            cost_open = sum(item.cost for item in self._open.values())
            return LedgerSnapshot(
                tokenizer=self._counter.identity,
                admissions=self._admissions,
                input_charged=self._input_charged,
                output_charged=self._output_charged,
                cost_charged=self._cost_charged,
                input_open=input_open,
                output_open=output_open,
                cost_open=cost_open,
                remaining_input=max(
                    0,
                    budget.max_input_tokens_per_request
                    - self._input_charged
                    - input_open,
                ),
                remaining_output=max(
                    0,
                    budget.max_output_tokens_per_request
                    - self._output_charged
                    - output_open,
                ),
                remaining_cost=max(
                    0,
                    self._policy.pricing.request_cost_cap
                    - self._cost_charged
                    - cost_open,
                ),
                input_drift_tokens=self._drift,
            )
