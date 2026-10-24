"""RequestTokenLedger: atomic cumulative caps, settlement, drift (D32)."""

from contextlib import suppress
from threading import Barrier, Thread

import pytest
from synthesis_support import (  # pyright: ignore[reportImplicitRelativeImport]
    COUNTER,
    SYNTH_POLICY,
    ledger,
)

from eventlens_rag.domain.model_input import (
    SYNTHESIS_INSTRUCTIONS,
    ChatMessage,
    ModelInput,
    SerializedModelInput,
    serialize_model_input,
)
from eventlens_rag.domain.synthesis import ProviderUsage
from eventlens_rag.domain.synthesis_errors import (
    BudgetFailureReason,
    SynthesisBudgetError,
    SynthesisConfigurationError,
)

INPUT_CAP = SYNTH_POLICY.budget.max_input_tokens_per_request
OUTPUT_CAP = SYNTH_POLICY.budget.max_output_tokens_per_request
PRICING = SYNTH_POLICY.pricing


def tokens(n: int) -> SerializedModelInput:
    """Serialized input whose counted text is exactly ``n`` fixture tokens."""
    return SerializedModelInput(
        serialization="synthesis-input-json.v1",
        prompt_version="synthesis-prompt.v1",
        messages=(),
        response_format_json="{}",
        tool_definitions=(),
        counted_text=" ".join("w" for _ in range(n)),
        sent_span_ids=frozenset(),
    )


def test_exact_input_cap_admits_and_one_over_refuses_without_recording() -> None:
    exact = ledger()
    assert exact.reserve(tokens(INPUT_CAP), 0).input_tokens == INPUT_CAP

    over = ledger()
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = over.reserve(tokens(INPUT_CAP + 1), 0)
    assert raised.value.reason is BudgetFailureReason.INPUT_CAP
    snapshot = over.snapshot()
    assert (snapshot.admissions, snapshot.input_open) == (0, 0)


def test_exact_output_cap_admits_and_one_over_refuses() -> None:
    assert ledger().reserve(tokens(1), OUTPUT_CAP).output_tokens == OUTPUT_CAP
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = ledger().reserve(tokens(1), OUTPUT_CAP + 1)
    assert raised.value.reason is BudgetFailureReason.OUTPUT_CAP


def test_single_call_at_full_caps_costs_exactly_the_cost_cap() -> None:
    request = ledger()
    reservation = request.reserve(tokens(INPUT_CAP), OUTPUT_CAP)
    assert reservation.cost == PRICING.request_cost_cap
    assert request.snapshot().remaining_cost == 0


def test_cost_cap_refuses_second_call_within_token_caps() -> None:
    request = ledger()
    first = request.reserve(tokens(6_000), 700)
    request.settle(first, "failure", None)
    # 2 x (6000 in, 700 out) stays within 12k/1.5k, but two fees exceed 20,000.
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = request.reserve(tokens(6_000), 700)
    assert raised.value.reason is BudgetFailureReason.COST_CAP


def test_input_cap_is_checked_before_output_and_cost() -> None:
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = ledger().reserve(tokens(INPUT_CAP + 1), OUTPUT_CAP + 1)
    assert raised.value.reason is BudgetFailureReason.INPUT_CAP


def test_two_calls_each_under_cap_accumulate_past_it() -> None:
    request = ledger()
    first = request.reserve(tokens(7_000), 100)
    request.settle(first, "success", None)
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = request.reserve(tokens(7_000), 100)
    assert raised.value.reason is BudgetFailureReason.INPUT_CAP


def test_failure_charges_full_reservation_and_never_refunds() -> None:
    request = ledger()
    reservation = request.reserve(tokens(1_000), 500)
    request.settle(reservation, "failure", ProviderUsage(1, 1))
    snapshot = request.snapshot()
    assert snapshot.input_charged == 1_000
    assert snapshot.output_charged == 500
    assert snapshot.cost_charged == reservation.cost
    assert snapshot.input_drift_tokens is None
    assert snapshot.remaining_input == INPUT_CAP - 1_000


def test_open_reservation_counts_against_concurrent_admissions() -> None:
    request = ledger()
    _ = request.reserve(tokens(8_000), 100)
    with pytest.raises(SynthesisBudgetError):
        _ = request.reserve(tokens(5_000), 100)


def test_success_charges_max_input_reported_output_and_records_drift() -> None:
    request = ledger()
    reservation = request.reserve(tokens(1_000), 1_500)
    request.settle(reservation, "success", ProviderUsage(1_120, 300))
    snapshot = request.snapshot()
    assert snapshot.input_charged == 1_120
    assert snapshot.output_charged == 300
    assert snapshot.cost_charged == (
        1_120 * PRICING.input_per_token
        + 300 * PRICING.output_per_token
        + PRICING.per_call_fee
    )
    assert snapshot.input_drift_tokens == 120


def test_under_reported_input_keeps_local_count_and_negative_drift() -> None:
    request = ledger()
    reservation = request.reserve(tokens(1_000), 100)
    request.settle(reservation, "success", ProviderUsage(900, 50))
    snapshot = request.snapshot()
    assert snapshot.input_charged == 1_000
    assert snapshot.input_drift_tokens == -100


@pytest.mark.parametrize(
    "usage",
    [ProviderUsage(-1, 10), ProviderUsage(10, -1), ProviderUsage(True, 3), None],  # noqa: FBT003
)
def test_missing_or_invalid_usage_charges_full_reservation(
    usage: ProviderUsage | None,
) -> None:
    request = ledger()
    reservation = request.reserve(tokens(1_000), 400)
    request.settle(reservation, "success", usage)
    snapshot = request.snapshot()
    assert (snapshot.input_charged, snapshot.output_charged) == (1_000, 400)
    assert snapshot.input_drift_tokens is None


def test_double_settle_is_an_error_not_a_second_charge() -> None:
    request = ledger()
    reservation = request.reserve(tokens(10), 10)
    request.settle(reservation, "failure", None)
    with pytest.raises(SynthesisConfigurationError):
        request.settle(reservation, "failure", None)
    assert request.snapshot().input_charged == 10


def test_concurrent_reservations_never_exceed_the_cap() -> None:
    request = ledger()
    workers = 12
    barrier = Barrier(workers)
    admitted: list[int] = []

    def worker() -> None:
        _ = barrier.wait()
        with suppress(SynthesisBudgetError):
            admitted.append(request.reserve(tokens(1_500), 0).input_tokens)

    threads = [Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    per_call = 1_500 * PRICING.input_per_token + PRICING.per_call_fee
    expected = min(INPUT_CAP // 1_500, PRICING.request_cost_cap // per_call)
    assert sum(admitted) <= INPUT_CAP
    assert len(admitted) == expected
    assert request.snapshot().cost_open <= PRICING.request_cost_cap


def _model_input(**fields: object) -> ModelInput:
    base: dict[str, object] = {
        "prompt_version": "synthesis-prompt.v1",
        "instructions": SYNTHESIS_INSTRUCTIONS,
        "question": "harbor",
        "sources": (),
    }
    base.update(fields)
    return ModelInput(**base)  # pyright: ignore[reportArgumentType]


def test_extra_messages_and_tool_definitions_are_counted() -> None:
    request = ledger()
    base = request.count(serialize_model_input(_model_input()))
    history = request.count(
        serialize_model_input(
            _model_input(extra_messages=(ChatMessage("assistant", "earlier turn"),))
        )
    )
    tools = request.count(
        serialize_model_input(_model_input(tool_definitions=('{"name":"lookup"}',)))
    )
    assert history > base
    assert tools > base


def test_multilingual_counts_use_tokenizer_not_characters() -> None:
    request = ledger()
    text = "港口关闭 항구 Ελλάδα Straße"
    serialized = serialize_model_input(_model_input(question=text))
    counted = request.count(serialized)
    assert counted == COUNTER.count(serialized.counted_text)
    assert counted != len(serialized.counted_text)
    assert request.identity == COUNTER.identity
