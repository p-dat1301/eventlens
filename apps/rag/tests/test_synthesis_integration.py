"""Phase 3: A (SynthesizeAnswer) composed with B (OpenAI adapter, mock transport).

No network: the transport is an injected recorder and sockets are blocked.
The live decision is built directly (live cost approval is out of T6 scope,
see interfaces.md section 7); the tokenizer is the fixture counter relabeled
with the live binding's identity so T5, ledger and decision share one mapping.
"""

from dataclasses import replace
from typing import override
from uuid import UUID

import pytest
from provider_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CHAT_URL,
    LIVE_TOKENIZER,
    RecordingTransport,
    completion,
    forbid_network,
    live_decision,
    live_policy,
    ok,
)
from pydantic import TypeAdapter
from retrieval_support import service  # pyright: ignore[reportImplicitRelativeImport]
from synthesis_support import (  # pyright: ignore[reportImplicitRelativeImport]
    CALLER,
    IDENTITY,
    SYNTH_POLICY,
    MemoryStore,
    MutableAccess,
    article,
    briefing,
    claim,
    control,
    draft,
    prepared_retrieval,
    sequential_ids,
    store_with,
    synthesizer,
)

from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.adapters.openai_synthesis import (
    OpenAICompatibleSynthesizer,
    TransportTimeoutError,
)
from eventlens_rag.adapters.scripted_synthesis import ScriptedSynthesizer
from eventlens_rag.application.concurrency import ProcessConcurrencyLimiter
from eventlens_rag.application.ledger import RequestTokenLedger
from eventlens_rag.application.retrieve import RetrievalOptions
from eventlens_rag.application.synthesize import PrepareSynthesis, SynthesizeAnswer
from eventlens_rag.domain.model_input import SYNTHESIS_INSTRUCTIONS
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.retrieval import RetrievalResult
from eventlens_rag.domain.synthesis import (
    SynthesisOutcome,
    SynthesisResult,
    UsageStatus,
)
from eventlens_rag.domain.synthesis_errors import (
    GroundingFailureCode,
    ProviderFailureCode,
    SynthesisConfigurationError,
    SynthesisGroundingError,
    SynthesisMalformedOutputError,
    SynthesisProviderError,
)
from eventlens_rag.ports.tokens import TokenizerIdentity

_OBJECT = TypeAdapter(dict[str, object])
_ROWS = TypeAdapter(list[dict[str, object]])
_FLOAT = TypeAdapter(float)
POLICY = live_policy()
DECISION = live_decision(POLICY)
QUESTION = "harbor crane inspection"


class LiveLabelCounter(FixtureTokenCounter):
    """Fixture counting under the test live tokenizer identity (tests only)."""

    @property
    @override
    def identity(self) -> TokenizerIdentity:
        return LIVE_TOKENIZER


COUNTER = LiveLabelCounter()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)


def _store() -> MemoryStore:
    return store_with(
        article("Harbor crane inspection closes the East Pier gate on Monday.")
    )


def _retrieve(
    store: MemoryStore, query: BriefingQuery
) -> tuple[RetrievalResult, RequestTokenLedger]:
    request_ledger = RequestTokenLedger(COUNTER, SYNTH_POLICY)
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, request_ledger)
    retrieval = replace(service(store), tokens=COUNTER).execute(
        query,
        CALLER,
        control(),
        RetrievalOptions(remaining_context_budget=plan.remaining_context_budget),
    )
    return retrieval, request_ledger


def _execute(
    transport: RecordingTransport,
    store: MemoryStore,
    query: BriefingQuery,
    retrieval: RetrievalResult,
    request_ledger: RequestTokenLedger,
) -> SynthesisResult:
    model = OpenAICompatibleSynthesizer(transport, DECISION, POLICY)
    use_case = SynthesizeAnswer(
        model=model,
        sources=store,
        access=MutableAccess(),
        tokens=COUNTER,
        policy=SYNTH_POLICY,
        activation=DECISION,
        ids=sequential_ids(),
    )
    lease = ProcessConcurrencyLimiter(30).try_acquire()
    try:
        return use_case.execute(
            query, retrieval, CALLER, control(), request_ledger, lease
        )
    finally:
        lease.release()


def _cited(retrieval: RetrievalResult) -> str:
    span = retrieval.articles[0].spans[0].span
    return draft([claim("c1", "The East Pier gate closes.", [span.span_id])])


def test_live_adapter_composes_with_service_and_maps_usage() -> None:
    store = _store()
    query = briefing(QUESTION)
    retrieval, request_ledger = _retrieve(store, query)
    usage = {"prompt_tokens": 1000, "completion_tokens": 40, "total_tokens": 1040}
    transport = RecordingTransport(
        [
            ok(
                completion(
                    _cited(retrieval), usage=usage, model="test-chat-model-2026-10-01"
                )
            )
        ]
    )

    result = _execute(transport, store, query, retrieval, request_ledger)

    assert result.outcome is SynthesisOutcome.ANSWERED
    assert len(result.answer.citations) == 1
    assert result.provenance.mode == "live"
    assert result.provenance.tokenizer == LIVE_TOKENIZER
    assert len(transport.calls) == 1
    call = _OBJECT.validate_python(transport.calls[0])
    assert call["url"] == CHAT_URL
    payload = _OBJECT.validate_python(call["payload"])
    messages = _ROWS.validate_python(payload["messages"])
    assert messages[0]["content"] == SYNTHESIS_INSTRUCTIONS
    assert [m["role"] for m in messages] == ["system", "user"]
    assert payload["max_tokens"] == SYNTH_POLICY.budget.output_reservation_per_call
    assert _FLOAT.validate_python(call["timeout"]) <= (
        SYNTH_POLICY.deadlines.call_timeout_seconds
    )
    counted = result.usage.input_tokens_counted
    assert result.usage.usage_status is UsageStatus.REPORTED
    assert result.usage.provider_input_tokens == 1000
    assert result.usage.input_drift_tokens == 1000 - counted
    assert result.usage.input_tokens_charged == max(counted, 1000)
    assert result.usage.output_tokens_charged == 40
    user = _OBJECT.validate_json(str(messages[1]["content"]))
    for source in _ROWS.validate_python(user["sources"]):
        assert "canonical_url" not in source
        assert "content_hash" not in source


def test_transport_timeout_settles_full_reservation_once() -> None:
    store = _store()
    query = briefing(QUESTION)
    retrieval, request_ledger = _retrieve(store, query)
    transport = RecordingTransport([TransportTimeoutError()])

    with pytest.raises(SynthesisProviderError) as raised:
        _ = _execute(transport, store, query, retrieval, request_ledger)

    assert raised.value.reason is ProviderFailureCode.TIMEOUT
    snapshot = request_ledger.snapshot()
    assert len(transport.calls) == 1
    assert snapshot.admissions == 1
    assert snapshot.input_open == snapshot.output_open == snapshot.cost_open == 0
    assert snapshot.output_charged == SYNTH_POLICY.budget.output_reservation_per_call


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (ok({"error": "BODY"}, status=500), None),
        (ok(completion(finish="length")), ProviderFailureCode.TRUNCATED),
        (ok(completion(refusal="no")), ProviderFailureCode.REFUSAL),
    ],
)
def test_adapter_failures_are_typed_settled_and_never_retried(
    step: object, code: ProviderFailureCode | None
) -> None:
    store = _store()
    query = briefing(QUESTION)
    retrieval, request_ledger = _retrieve(store, query)
    transport = RecordingTransport([step])  # pyright: ignore[reportArgumentType]

    with pytest.raises(SynthesisProviderError) as raised:
        _ = _execute(transport, store, query, retrieval, request_ledger)

    if code is not None:
        assert raised.value.reason is code
    assert "BODY" not in str(raised.value)
    assert len(transport.calls) == 1
    assert request_ledger.snapshot().output_open == 0


def test_invalid_span_and_malformed_content_fail_without_raw_fallback() -> None:
    store = _store()
    query = briefing(QUESTION)
    retrieval, request_ledger = _retrieve(store, query)
    fake = UUID("00000000-0000-4000-8000-00000000dead")
    bad = draft([claim("c1", "RAW-PROSE-CANARY", [fake])])
    transport = RecordingTransport([ok(completion(bad))])
    with pytest.raises(SynthesisGroundingError) as grounding:
        _ = _execute(transport, store, query, retrieval, request_ledger)
    assert grounding.value.reason is GroundingFailureCode.UNKNOWN_SPAN
    assert "RAW-PROSE-CANARY" not in str(grounding.value)

    retrieval, request_ledger = _retrieve(store, query)
    transport = RecordingTransport([ok(completion("RAW-PROSE-CANARY not json"))])
    with pytest.raises(SynthesisMalformedOutputError) as malformed:
        _ = _execute(transport, store, query, retrieval, request_ledger)
    assert "RAW-PROSE-CANARY" not in str(malformed.value)


def test_retrieval_counted_with_another_tokenizer_is_rejected_before_call() -> None:
    """Regression (phase 3): T5 and T6 must share one TokenCounter mapping."""
    store = _store()
    query = briefing(QUESTION)
    retrieval = service(store).execute(query, CALLER, control())  # fixture identity
    transport = RecordingTransport([])

    with pytest.raises(SynthesisConfigurationError):
        _ = _execute(
            transport,
            store,
            query,
            retrieval,
            RequestTokenLedger(COUNTER, SYNTH_POLICY),
        )

    assert transport.calls == []


def test_typed_non_provider_adapter_error_keeps_code_and_settles() -> None:
    """Regression (phase 3, request B-4): any SynthesisError from the port is
    re-raised as-is and its reservation is settled as a failure."""
    store = store_with(
        article("Harbor crane inspection closes the East Pier gate on Monday.")
    )
    query = briefing(QUESTION)
    retrieval, request_ledger = prepared_retrieval(store, query)
    model = ScriptedSynthesizer(IDENTITY, [SynthesisConfigurationError("x")])
    lease = ProcessConcurrencyLimiter(30).try_acquire()

    with pytest.raises(SynthesisConfigurationError):
        _ = synthesizer(model, store).execute(
            query, retrieval, CALLER, control(), request_ledger, lease
        )

    lease.release()
    snapshot = request_ledger.snapshot()
    assert snapshot.admissions == 1
    assert snapshot.output_open == 0
    assert snapshot.output_charged == SYNTH_POLICY.budget.output_reservation_per_call


def test_provider_reporting_model_version_is_accepted_end_to_end() -> None:
    """Regression (phase 3): B accepts model_id or model_version; A must too."""
    store = _store()
    query = briefing(QUESTION)
    retrieval, request_ledger = _retrieve(store, query)
    version = DECISION.chat_model.model_version
    transport = RecordingTransport([ok(completion(_cited(retrieval), model=version))])

    result = _execute(transport, store, query, retrieval, request_ledger)

    assert result.outcome is SynthesisOutcome.ANSWERED
