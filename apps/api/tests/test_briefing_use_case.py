"""T7 use case + compiled briefing graph (fixture corpus, fake model only)."""

import subprocess
import sys
from pathlib import Path
from threading import Barrier, Thread
from uuid import UUID

import pytest
from briefing_support import (  # pyright: ignore[reportImplicitRelativeImport]
    NO_MATCH,
    QUESTION,
    Harness,
    draft_citing,
    harness,
    query,
    raising,
    respond,
    returning,
    revise,
)

from eventlens_api.briefing import (
    BriefingConfigurationError,
    BriefingServices,
)
from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.application.concurrency import ProcessConcurrencyLimiter
from eventlens_rag.domain.retrieval import (
    QueryControl,
    RetrievalStorageError,
)
from eventlens_rag.domain.synthesis import SynthesisOutcome
from eventlens_rag.domain.synthesis_errors import (
    BudgetFailureReason,
    GroundingFailureCode,
    MalformedOutputReason,
    ProviderFailureCode,
    SynthesisAccessDeniedError,
    SynthesisBudgetError,
    SynthesisCancelledError,
    SynthesisConcurrencyRejectedError,
    SynthesisDeadlineExceededError,
    SynthesisEvidenceStaleError,
    SynthesisGroundingError,
    SynthesisMalformedOutputError,
    SynthesisProviderError,
)
from eventlens_rag.graph.briefing import BriefingGraph
from eventlens_rag.ports.briefing import BriefingRoute
from eventlens_rag.ports.synthesis import SynthesisRequest, SynthesisResponse

ROOT = Path(__file__).parents[3]
UNKNOWN_SPAN = UUID("00000000-0000-4000-8000-00000000dead")


def _routes(h: Harness) -> list[str]:
    return [event.route for event in h.telemetry.events]


def test_valid_briefing_returns_grounded_claims_and_citations() -> None:
    # Given
    h = harness()

    # When
    outcome = h.runtime.use_case.execute(query())

    # Then
    answer = outcome.result.answer
    assert outcome.route is BriefingRoute.ANSWERED
    assert outcome.result.outcome is SynthesisOutcome.ANSWERED
    assert answer.claims
    assert len(outcome.cited_sources) == len(answer.citations)
    claim_ids = {claim.claim_id for claim in answer.claims}
    for cited in outcome.cited_sources:
        assert cited.citation.claim_id in claim_ids
        assert cited.span.span_id == cited.citation.evidence_span_id
        document = h.store.documents[cited.citation.article_id]
        assert (
            document.normalized_content[cited.span.start_offset : cited.span.end_offset]
            == cited.span.excerpt
        )
    assert h.model.calls == 1
    assert h.runtime.use_case.limiter.in_use == 0
    assert _routes(h) == ["answered"]


def test_no_evidence_abstains_without_model_call() -> None:
    # Given
    h = harness()

    # When
    outcome = h.runtime.use_case.execute(query(NO_MATCH))

    # Then
    assert outcome.route is BriefingRoute.ABSTAINED_NO_EVIDENCE
    assert outcome.result.outcome is SynthesisOutcome.ABSTAINED_NO_EVIDENCE
    assert outcome.result.answer.claims == ()
    assert outcome.result.answer.uncertainties
    assert h.model.calls == 0


def test_lexical_only_retrieval_answers_before_enrichment_is_ready() -> None:
    # Given: fixture corpus with no READY metadata and no vectors.
    h = harness()

    # When
    outcome = h.runtime.use_case.execute(query())

    # Then
    assert outcome.retrieval.retrieval_mode.value == "lexical_only"
    assert outcome.result.outcome is SynthesisOutcome.ANSWERED


def test_hybrid_retrieval_when_vectors_are_active() -> None:
    # Given
    h = harness(vectors=True)

    # When
    outcome = h.runtime.use_case.execute(query())

    # Then
    assert outcome.retrieval.retrieval_mode.value == "hybrid"
    assert outcome.result.outcome is SynthesisOutcome.ANSWERED


def test_retrieval_storage_unavailable_is_typed_and_routed() -> None:
    # Given
    h = harness()
    h.store.available = False

    # When / Then
    with pytest.raises(RetrievalStorageError):
        _ = h.runtime.use_case.execute(query())
    assert h.model.calls == 0
    assert _routes(h) == ["retrieval_failed"]
    assert h.runtime.use_case.limiter.in_use == 0


@pytest.mark.parametrize(
    "code", [ProviderFailureCode.UNAVAILABLE, ProviderFailureCode.TIMEOUT]
)
def test_model_unavailable_or_timeout_is_typed(code: ProviderFailureCode) -> None:
    # Given
    h = harness(behaviour=raising(SynthesisProviderError(code)))

    # When / Then
    with pytest.raises(SynthesisProviderError) as caught:
        _ = h.runtime.use_case.execute(query())
    assert caught.value.reason is code
    assert h.model.calls == 1
    assert _routes(h) == ["synthesis_failed"]


def test_malformed_model_output_is_rejected() -> None:
    # Given
    h = harness(behaviour=returning("not json {"))

    # When / Then
    with pytest.raises(SynthesisMalformedOutputError) as caught:
        _ = h.runtime.use_case.execute(query())
    assert caught.value.reason is MalformedOutputReason.INVALID_JSON


def test_unknown_citation_span_is_rejected() -> None:
    # Given
    h = harness(behaviour=returning(draft_citing(UNKNOWN_SPAN)))

    # When / Then
    with pytest.raises(SynthesisGroundingError) as caught:
        _ = h.runtime.use_case.execute(query())
    assert caught.value.reason is GroundingFailureCode.UNKNOWN_SPAN


def test_article_edited_during_call_fails_hash_check() -> None:
    # Given: the cited article gets a new revision while the model runs.
    h = harness()

    def edit_then_answer(request: SynthesisRequest) -> SynthesisResponse:
        for document in tuple(h.store.documents.values()):
            h.store.add(revise(document))
        return h.model.fake(request)

    h.model.behaviour = edit_then_answer

    # When / Then
    with pytest.raises(SynthesisEvidenceStaleError):
        _ = h.runtime.use_case.execute(query())


def test_budget_failure_happens_before_storage_and_model_work() -> None:
    # Given
    h = harness()

    # When / Then
    with pytest.raises(SynthesisBudgetError) as caught:
        _ = h.runtime.use_case.execute(query(context_budget=8))
    assert caught.value.reason is BudgetFailureReason.CONTEXT_TOO_SMALL
    assert h.store.reads == 0
    assert h.model.calls == 0
    assert _routes(h) == ["budget_exceeded"]


def test_access_denied_before_any_work() -> None:
    # Given
    h = harness()
    h.access.revoked = True

    # When / Then
    with pytest.raises(SynthesisAccessDeniedError):
        _ = h.runtime.use_case.execute(query())
    assert h.store.reads == 0
    assert h.model.calls == 0
    assert _routes(h) == ["access_denied"]


def test_access_revoked_during_model_call_returns_no_answer() -> None:
    # Given
    h = harness()

    def revoke(request: SynthesisRequest) -> SynthesisResponse:
        h.access.revoked = True
        return h.model.fake(request)

    h.model.behaviour = revoke

    # When / Then
    with pytest.raises(SynthesisAccessDeniedError):
        _ = h.runtime.use_case.execute(query())


def test_cancelled_request_does_no_storage_or_model_work() -> None:
    # Given
    h = harness()
    control = h.runtime.use_case.new_control()
    control.cancel()

    # When / Then
    with pytest.raises(SynthesisCancelledError):
        _ = h.runtime.use_case.execute(query(), control=control)
    assert h.store.reads == 0
    assert h.model.calls == 0
    assert h.runtime.use_case.limiter.in_use == 0


def test_expired_deadline_does_no_storage_or_model_work() -> None:
    # Given
    h = harness()
    control = QueryControl(0.0)

    # When / Then
    with pytest.raises(SynthesisDeadlineExceededError):
        _ = h.runtime.use_case.execute(query(), control=control)
    assert h.store.reads == 0
    assert h.model.calls == 0


def test_cancel_during_model_call_drops_late_result_and_stops_reads() -> None:
    # Given
    h = harness()
    control = h.runtime.use_case.new_control()
    reads_at_call: list[int] = []

    def cancel_then_answer(request: SynthesisRequest) -> SynthesisResponse:
        reads_at_call.append(h.store.reads)
        control.cancel()
        return h.model.fake(request)

    h.model.behaviour = cancel_then_answer

    # When / Then
    with pytest.raises(SynthesisCancelledError):
        _ = h.runtime.use_case.execute(query(), control=control)
    assert h.model.calls == 1
    assert h.store.reads == reads_at_call[0]  # no post-call batch read
    assert _routes(h) == ["cancelled"]
    assert h.runtime.use_case.limiter.in_use == 0


def test_capacity_exhausted_rejects_before_work() -> None:
    # Given
    limiter = ProcessConcurrencyLimiter(30)
    h = harness(limiter=limiter)
    leases = [limiter.try_acquire() for _ in range(30)]

    # When / Then
    try:
        with pytest.raises(SynthesisConcurrencyRejectedError):
            _ = h.runtime.use_case.execute(query())
    finally:
        for lease in leases:
            lease.release()
    assert h.store.reads == 0


def test_two_invocations_are_isolated() -> None:
    # Given
    h = harness()

    # When
    first = h.runtime.use_case.execute(query())
    second = h.runtime.use_case.execute(query(NO_MATCH))
    third = h.runtime.use_case.execute(query())

    # Then: no state leaks between invocations of the one compiled graph.
    assert first.invocation_id != third.invocation_id
    assert second.result.answer.claims == ()
    assert second.cited_sources == ()
    assert first.result.usage == third.result.usage  # fresh ledger each time
    assert {c.claim_id for c in first.result.answer.claims}.isdisjoint(
        {c.claim_id for c in third.result.answer.claims}
    )
    assert {s.source_ref for s in first.cited_sources}.isdisjoint(
        {s.source_ref for s in third.cited_sources}
    )


def test_concurrent_invocations_are_isolated() -> None:
    # Given: two requests inside the model call at the same time.
    h = harness()
    barrier = Barrier(2, timeout=5)

    def meet(request: SynthesisRequest) -> SynthesisResponse:
        _ = barrier.wait()
        return h.model.fake(request)

    h.model.behaviour = meet
    results: dict[str, object] = {}

    def call(name: str, text: str) -> None:
        results[name] = h.runtime.use_case.execute(query(text))

    threads = [
        Thread(target=call, args=("a", QUESTION)),
        Thread(target=call, args=("b", "Which gate did cargo trucks use?")),
    ]

    # When
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    # Then
    assert set(results) == {"a", "b"}
    assert h.runtime.use_case.limiter.in_use == 0
    assert h.model.calls == 2
    questions = {r.serialized.messages[1].content for r in h.model.requests}
    assert len(questions) == 2


def test_graph_compiles_once_with_every_route() -> None:
    # Given
    h = harness()

    # Then
    assert isinstance(h.runtime.use_case.workflow, BriefingGraph)
    assert h.runtime.use_case.workflow is h.runtime.graph
    assert set(h.runtime.graph.node_names) == {
        "prepare",
        "retrieve",
        "synthesize",
        "abstain",
        "answered",
        "abstained",
        "retrieval_failed",
        "synthesis_failed",
        "timed_out",
        "cancelled",
        "budget_exceeded",
        "access_denied",
    }


def test_composition_rejects_split_token_counters() -> None:
    # Given
    services = harness().runtime.use_case.services

    # When / Then
    with pytest.raises(BriefingConfigurationError):
        _ = BriefingServices(
            retriever=services.retriever,
            synthesizer=services.synthesizer,
            access=services.access,
            tokens=FixtureTokenCounter(),
            policy=services.policy,
        )


def test_model_abstention_maps_to_its_own_route() -> None:
    # Given
    content = (
        '{"schema_version":"synthesis_draft.v1","evidence_status":"insufficient",'
        '"claims":[],"uncertainties":[{"reason_code":"insufficient_evidence",'
        '"claim_ref":null}]}'
    )
    h = harness(behaviour=lambda _: respond(content))

    # When
    outcome = h.runtime.use_case.execute(query())

    # Then
    assert outcome.route is BriefingRoute.ABSTAINED_BY_MODEL
    assert outcome.result.answer.claims == ()


def test_application_layer_stays_framework_free() -> None:
    # Given
    source = ROOT / "apps/api/src/eventlens_api"
    forbidden = (
        "fastapi",
        "langgraph",
        "psycopg",
        "eventlens_worker",
        "eventlens_rag.graph",
        "eventlens_rag.adapters",
    )

    # When
    violations = [
        (name, token)
        for name in ("briefing.py", "sources.py", "application.py")
        for token in forbidden
        if token in (source / name).read_text(encoding="utf-8")
    ]
    http_violations = [
        token
        for token in (
            "psycopg",
            "SELECT",
            "langgraph",
            "StateGraph",
            "eventlens_worker",
        )
        for name in ("briefing_http.py", "http.py")
        if token in (source / name).read_text(encoding="utf-8")
    ]

    # Then
    assert violations == []
    assert http_violations == []


def test_api_never_imports_worker_at_runtime() -> None:
    # Given
    code = (
        "import sys; import eventlens_api.main; "
        "assert not any(m.startswith('eventlens_worker') for m in sys.modules)"
    )

    # When
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal code
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    # Then
    assert result.returncode == 0, result.stderr
