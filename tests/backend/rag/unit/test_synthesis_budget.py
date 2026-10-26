"""D32 budget wiring: preparation, recount/trim, caps, call cap, drift."""

import json
from dataclasses import dataclass

import pytest

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ledger import RequestTokenLedger
from backend.rag.application.ports.synthesis import SynthesisModelPort
from backend.rag.application.use_cases.synthesize import (
    PrepareSynthesis,
    SynthesizeAnswer,
)
from backend.rag.domain.model_input import (
    SYNTHESIS_INSTRUCTIONS,
    ModelInput,
    serialize_model_input,
    source_payload,
)
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import RetrievalResult
from backend.rag.domain.synthesis import ProviderUsage, SynthesisResult, UsageStatus
from backend.rag.domain.synthesis_errors import (
    BudgetFailureReason,
    GroundingFailureCode,
    SynthesisBudgetError,
    SynthesisConfigurationError,
    SynthesisGroundingError,
)
from backend.rag.domain.synthesis_policy import SynthesisPolicy
from backend.rag.domain.tokenizer import TokenizerIdentity
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from synthesis_support import (
    CALLER,
    COUNTER,
    DECISION,
    IDENTITY,
    SYNTH_POLICY,
    MemoryStore,
    MutableAccess,
    article,
    briefing,
    claim,
    control,
    draft,
    ledger,
    prepared_retrieval,
    respond,
    retrieve,
    run,
    sequential_ids,
    store_with,
)

INPUT_CAP = SYNTH_POLICY.budget.max_input_tokens_per_request
FRAMING = SYNTH_POLICY.budget.framing_reserve_tokens
MIN_EVIDENCE = SYNTH_POLICY.budget.min_evidence_tokens


def variant(**budget: int) -> SynthesisPolicy:
    """Negative/boundary-case policy built only through validation."""
    body = SYNTH_POLICY.model_dump(mode="json")
    body["budget"] = {**body["budget"], **budget}
    return SynthesisPolicy.model_validate_json(json.dumps(body))


def header_tokens(query: BriefingQuery) -> int:
    return COUNTER.count(
        serialize_model_input(
            ModelInput(
                prompt_version=SYNTH_POLICY.prompt_version,
                instructions=SYNTHESIS_INSTRUCTIONS,
                question=query.query,
                sources=(),
            )
        ).counted_text
    )


def payload_tokens(query: BriefingQuery, retrieval: RetrievalResult, keep: int) -> int:
    """Tokens of the serialized payload holding the first ``keep`` articles."""
    return COUNTER.count(
        serialize_model_input(
            ModelInput(
                prompt_version=SYNTH_POLICY.prompt_version,
                instructions=SYNTHESIS_INSTRUCTIONS,
                question=query.query,
                sources=tuple(
                    source_payload(item, item.spans)
                    for item in retrieval.articles[:keep]
                ),
            )
        ).counted_text
    )


def run_with(  # noqa: PLR0913, PLR0917
    policy: SynthesisPolicy,
    model: SynthesisModelPort,
    store: MemoryStore,
    query: BriefingQuery,
    retrieval: RetrievalResult,
    request_ledger: RequestTokenLedger | None = None,
) -> SynthesisResult:
    lease = ProcessConcurrencyLimiter(30).try_acquire()
    try:
        return SynthesizeAnswer(
            model=model,
            sources=store,
            access=MutableAccess(),
            tokens=COUNTER,
            policy=policy,
            activation=DECISION,
            ids=sequential_ids(),
        ).execute(
            query,
            retrieval,
            CALLER,
            control(),
            request_ledger or RequestTokenLedger(COUNTER, policy),
            lease,
        )
    finally:
        lease.release()


def _store() -> MemoryStore:
    return store_with(
        article("Harbor bridge closed after the storm.", title="Bridge"),
        article("Harbor ferries resumed service today.", title="Ferry"),
        article("Harbor cranes were inspected overnight.", title="Cranes"),
    )


def _echo() -> ScriptedSynthesizer:
    fake = DeterministicFakeSynthesizer(IDENTITY)
    return ScriptedSynthesizer(IDENTITY, [fake.synthesize])


# Preparation (before T5) ------------------------------------------------


def test_plan_subtracts_full_serialized_header_and_framing_reserve() -> None:
    query = briefing("harbor bridge")
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger())

    header = header_tokens(query)
    assert plan.header_tokens == header
    # The header is far more than question + instructions: schema and wrappers.
    assert header > COUNTER.count(query.query) + COUNTER.count(SYNTHESIS_INSTRUCTIONS)
    assert plan.remaining_context_budget == min(
        query.context_budget, INPUT_CAP - header - FRAMING
    )
    assert plan.framing_reserve_tokens == FRAMING
    assert plan.output_reservation == SYNTH_POLICY.budget.output_reservation_per_call
    assert plan.tokenizer == COUNTER.identity


def test_low_query_budget_is_passed_to_t5_unchanged() -> None:
    store = _store()
    query = briefing("harbor", context_budget=300)
    retrieval, request_ledger = prepared_retrieval(store, query)

    assert retrieval.context.token_budget == 300
    assert retrieval.context.token_count <= 300
    result = run(_echo(), store, query, retrieval, request_ledger=request_ledger)
    assert result.usage.model_calls == 1


def test_prior_charges_lower_the_allowance_for_t5() -> None:
    query = briefing("harbor")
    fresh = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger())
    used = ledger()
    reservation = used.reserve(serialize_model_input(_header_input(query)), 0)
    used.settle(reservation, "failure", None)

    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, used)

    assert plan.remaining_context_budget == min(
        query.context_budget,
        fresh.remaining_context_budget - reservation.input_tokens,
    )


def _header_input(query: BriefingQuery) -> ModelInput:
    return ModelInput(
        prompt_version=SYNTH_POLICY.prompt_version,
        instructions=SYNTHESIS_INSTRUCTIONS,
        question=query.query,
        sources=(),
    )


def _question_for(target_header: int) -> str:
    base = header_tokens(briefing("w"))
    return " ".join("w" for _ in range(target_header - base + 1))


def test_header_larger_than_input_cap_fails_before_retrieval() -> None:
    query = briefing(_question_for(INPUT_CAP + 1))
    assert header_tokens(query) == INPUT_CAP + 1

    with pytest.raises(SynthesisBudgetError) as raised:
        _ = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger())
    assert raised.value.reason is BudgetFailureReason.HEADER_EXCEEDS_INPUT


def test_header_exactly_at_cap_is_context_too_small_not_header_failure() -> None:
    query = briefing(_question_for(INPUT_CAP))
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger())
    assert raised.value.reason is BudgetFailureReason.CONTEXT_TOO_SMALL


def test_min_evidence_tokens_boundary_is_wired() -> None:
    fits = briefing(_question_for(INPUT_CAP - FRAMING - MIN_EVIDENCE))
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(fits, ledger())
    assert plan.remaining_context_budget == MIN_EVIDENCE

    short = briefing(_question_for(INPUT_CAP - FRAMING - MIN_EVIDENCE + 1))
    with pytest.raises(SynthesisBudgetError) as raised:
        _ = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(short, ledger())
    assert raised.value.reason is BudgetFailureReason.CONTEXT_TOO_SMALL


def test_framing_reserve_value_is_read_from_policy() -> None:
    query = briefing("harbor", context_budget=12_000)
    wide = PrepareSynthesis(COUNTER, variant(framing_reserve_tokens=0)).execute(
        query, ledger()
    )
    narrow = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger())
    assert wide.remaining_context_budget - narrow.remaining_context_budget == FRAMING


@dataclass(frozen=True, slots=True)
class OtherCounter:
    @property
    def identity(self) -> TokenizerIdentity:
        return TokenizerIdentity("other-tokenizer", "v1")

    def count(self, text: str) -> int:
        return len(text.split())

    def prefix_end(self, text: str, max_tokens: int) -> int:
        del max_tokens
        return len(text)


def test_tokenizer_mismatch_fails_closed_in_prepare_and_execute() -> None:
    query = briefing("harbor")
    foreign = RequestTokenLedger(OtherCounter(), SYNTH_POLICY)
    with pytest.raises(SynthesisConfigurationError):
        _ = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, foreign)

    store = _store()
    model = _echo()
    with pytest.raises(SynthesisConfigurationError):
        _ = run(model, store, query, retrieve(store, query), request_ledger=foreign)
    assert model.requests == []


# Pre-call recount, trim and caps ---------------------------------------


def test_payload_exactly_at_cap_sends_everything() -> None:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    full = payload_tokens(query, retrieval, len(retrieval.articles))
    model = _echo()

    result = run_with(
        variant(max_input_tokens_per_request=full), model, store, query, retrieval
    )

    sent = model.requests[0].serialized
    assert COUNTER.count(sent.counted_text) == full
    assert result.usage.input_tokens_counted == full
    assert result.metrics.articles_sent == len(retrieval.articles)


def test_one_over_cap_drops_whole_trailing_span_and_it_cannot_be_cited() -> None:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    full = payload_tokens(query, retrieval, len(retrieval.articles))
    dropped = retrieval.articles[-1].spans[-1].span
    content = draft([claim("c1", "Cranes inspected.", [dropped.span_id])])
    model = ScriptedSynthesizer(IDENTITY, [respond(content)])

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = run_with(
            variant(max_input_tokens_per_request=full - 1),
            model,
            store,
            query,
            retrieval,
        )

    sent = model.requests[0].serialized
    assert dropped.span_id not in sent.sent_span_ids
    assert dropped.excerpt not in sent.messages[1].content
    assert COUNTER.count(sent.counted_text) <= full - 1
    assert raised.value.reason is GroundingFailureCode.SPAN_NOT_SENT


def test_no_span_fits_is_typed_budget_failure_with_zero_calls() -> None:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    model = _echo()

    with pytest.raises(SynthesisBudgetError) as raised:
        _ = run_with(
            variant(max_input_tokens_per_request=header_tokens(query)),
            model,
            store,
            query,
            retrieval,
        )
    assert raised.value.reason is BudgetFailureReason.EVIDENCE_EXCEEDS_BUDGET
    assert model.requests == []


def test_second_execute_on_same_request_hits_call_cap_with_zero_calls() -> None:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    request_ledger = ledger()
    model = ScriptedSynthesizer(
        IDENTITY, [DeterministicFakeSynthesizer(IDENTITY).synthesize] * 2
    )
    _ = run(model, store, query, retrieval, request_ledger=request_ledger)

    with pytest.raises(SynthesisBudgetError) as raised:
        _ = run(model, store, query, retrieval, request_ledger=request_ledger)

    assert raised.value.reason is BudgetFailureReason.CALL_CAP
    assert len(model.requests) == 1


def test_output_reservation_is_read_from_policy_and_sent_as_max_tokens() -> None:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    default = _echo()
    _ = run(default, store, query, retrieval)
    smaller = _echo()
    _ = run_with(
        variant(output_reservation_per_call=700), smaller, store, query, retrieval
    )

    assert default.requests[0].max_output_tokens == 1_500
    assert smaller.requests[0].max_output_tokens == 700


def test_output_cap_exhausted_refuses_before_retrieval() -> None:
    used = ledger()
    reservation = used.reserve(serialize_model_input(_header_input(briefing("x"))), 1)
    used.settle(reservation, "failure", None)

    with pytest.raises(SynthesisBudgetError) as raised:
        _ = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(briefing("harbor"), used)
    assert raised.value.reason is BudgetFailureReason.OUTPUT_CAP


def test_max_articles_cap_is_read_from_policy() -> None:
    store = store_with(
        *(article(f"Harbor item {n} reported.", title=f"T{n}") for n in range(4))
    )
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    model = _echo()

    result = run_with(variant(max_articles=2), model, store, query, retrieval)

    assert result.metrics.articles_sent == 2


def test_bundled_cap_sends_up_to_twenty_articles() -> None:
    store = store_with(
        *(article(f"Harbor item {n} reported.", title=f"T{n}") for n in range(20))
    )
    query = briefing("harbor", context_budget=12_000)
    retrieval, request_ledger = prepared_retrieval(store, query)
    model = _echo()

    result = run(model, store, query, retrieval, request_ledger=request_ledger)

    assert result.metrics.articles_sent == len(retrieval.articles)
    assert result.usage.input_tokens_charged <= INPUT_CAP


# Usage accounting and drift --------------------------------------------


def _scripted_usage(
    usage: ProviderUsage | None, status: UsageStatus
) -> tuple[SynthesisResult, ScriptedSynthesizer]:
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    span = retrieval.articles[0].spans[0].span.span_id
    content = draft([claim("c1", "Bridge closed.", [span])])
    model = ScriptedSynthesizer(
        IDENTITY, [respond(content, usage=usage, status=status)]
    )
    return run(model, store, query, retrieval), model


def test_reported_usage_is_charged_and_drift_recorded() -> None:
    probe, _ = _scripted_usage(None, UsageStatus.MISSING)
    local = probe.usage.input_tokens_counted
    result, _ = _scripted_usage(ProviderUsage(local + 40, 120), UsageStatus.REPORTED)

    usage = result.usage
    assert usage.usage_status is UsageStatus.REPORTED
    assert usage.provider_input_tokens == local + 40
    assert usage.input_tokens_charged == local + 40
    assert usage.output_tokens_charged == 120
    assert usage.input_drift_tokens == 40
    pricing = SYNTH_POLICY.pricing
    assert usage.cost_charged == (
        (local + 40) * pricing.input_per_token
        + 120 * pricing.output_per_token
        + pricing.per_call_fee
    )
    assert usage.cost_unit == pricing.unit


def test_invalid_reported_usage_is_flagged_and_charges_full_reservation() -> None:
    result, _ = _scripted_usage(ProviderUsage(-5, 10), UsageStatus.REPORTED)

    usage = result.usage
    assert usage.usage_status is UsageStatus.INVALID
    assert usage.provider_input_tokens is None
    assert usage.input_drift_tokens is None
    assert usage.output_tokens_charged == usage.output_tokens_reserved == 1_500


def test_missing_usage_charges_full_output_reservation() -> None:
    result, _ = _scripted_usage(None, UsageStatus.MISSING)
    assert result.usage.usage_status is UsageStatus.MISSING
    assert result.usage.output_tokens_charged == 1_500
