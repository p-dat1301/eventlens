"""Rerank stage (D43): identity/scripted fakes, limits, degrade and stop rules."""

import math
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest

from backend.rag.application.ports.reranker import RerankOutput, RerankRequest
from backend.rag.application.use_cases.retrieve import RetrievalOptions
from backend.rag.domain.retrieval import (
    QueryControl,
    RerankDegradeReason,
    RerankStatus,
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalDeadlineExceededError,
    RetrievalResult,
)
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.retrieval.identity_reranker import IdentityReranker
from backend.rag.infra.retrieval.scripted_reranker import (
    ScriptedReranker,
    failing_script,
    reverse_script,
)
from retrieval_support import (
    CALLER,
    POLICY,
    FakeMonotonic,
    MemoryStore,
    MutableAccess,
    article,
    briefing,
    control,
    service,
)

LEXICAL = RetrievalOptions(vector_enabled=False)


def corpus(count: int, *, same_time: bool = False) -> MemoryStore:
    store = MemoryStore()
    start = datetime(2026, 3, 1, tzinfo=UTC)
    for index in range(count):
        published = start if same_time else start + timedelta(hours=index)
        store.add(
            article(
                f"Harbor crane inspection bulletin {index} {'x' * (index % 7)}.",
                published=published,
            )
        )
    return store


def order(result: RetrievalResult) -> list[str]:
    return [str(a.article_id) for a in result.articles]


def run(
    store: MemoryStore,
    reranker: ScriptedReranker | IdentityReranker | None,
    ctl: QueryControl | None = None,
    options: RetrievalOptions = LEXICAL,
    access: MutableAccess | None = None,
) -> RetrievalResult:
    retrieve = service(
        store,
        embeddings=None,
        reranker=reranker,
        use_default_reranker=False,
        access=access,
    )
    return retrieve.execute(
        briefing("harbor crane inspection"), CALLER, ctl or control(), options
    )


def test_identity_reranker_keeps_fused_order_including_ties() -> None:
    store = corpus(12, same_time=True)  # equal scores -> pure tie-break order

    skipped = run(store, None)
    identity = run(store, IdentityReranker())

    assert skipped.rerank_status is RerankStatus.SKIPPED
    assert identity.rerank_status is RerankStatus.APPLIED
    assert identity.provenance.reranker_id == "identity-reranker"
    assert order(identity) == order(skipped)


def test_disabled_reranker_is_skipped_and_not_called() -> None:
    scripted = ScriptedReranker(reverse_script)

    result = run(
        corpus(5),
        scripted,
        options=RetrievalOptions(vector_enabled=False, rerank_enabled=False),
    )

    assert result.rerank_status is RerankStatus.SKIPPED
    assert scripted.requests == []
    assert result.metrics.rerank_calls == 0


def test_scripted_reranker_reorders_only_the_known_candidate_set() -> None:
    store = corpus(6)
    fused = run(store, None)
    scripted = ScriptedReranker(reverse_script)

    result = run(store, scripted)

    assert result.rerank_status is RerankStatus.APPLIED
    assert order(result) == list(reversed(order(fused)))
    assert set(order(result)) == set(order(fused))
    sent = {c.candidate_id for c in scripted.requests[0].candidates}
    returned = {s.chunk_id for a in result.articles for s in a.spans}
    assert returned <= sent


def test_rerank_window_is_40_candidates_of_at_most_512_tokens() -> None:
    store = MemoryStore()
    for index in range(60):
        # CJK runs are single \w+ terms, so the query term is space-delimited.
        store.add(article(f"港口 update{index} " + "港" * 1300))
    scripted = ScriptedReranker(reverse_script)
    counter = FixtureTokenCounter()

    result = service(store, embeddings=None, reranker=scripted).execute(
        briefing("港口"), CALLER, control(), LEXICAL
    )

    del result
    request = scripted.requests[0]
    assert len(request.candidates) <= 40
    for candidate in request.candidates:
        assert counter.count(candidate.text) <= 512
        assert counter.count(candidate.text) == 512
        assert any(  # exact, unmodified prefix of a stored chunk
            doc.normalized_content.startswith(candidate.text)
            for doc in store.documents.values()
        )


def test_window_tail_beyond_40_keeps_fused_order_after_reranked_block() -> None:
    store = corpus(45)
    scripted = ScriptedReranker(reverse_script)

    result = service(store, embeddings=None, reranker=scripted).execute(
        briefing("harbor crane inspection", result_limit=20), CALLER, control(), LEXICAL
    )

    assert len(scripted.requests[0].candidates) == 40
    assert result.rerank_status is RerankStatus.APPLIED
    assert len(result.articles) == 20


def bad_unknown(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=(*((c.candidate_id, 1.0) for c in request.candidates), (uuid4(), 9.0))
    )


def bad_duplicate(request: RerankRequest) -> RerankOutput:
    first = request.candidates[0].candidate_id
    return RerankOutput(
        scores=(*((c.candidate_id, 1.0) for c in request.candidates), (first, 2.0))
    )


def bad_missing(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple((c.candidate_id, 1.0) for c in request.candidates[1:])
    )


def bad_nan(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple((c.candidate_id, math.nan) for c in request.candidates)
    )


@pytest.mark.parametrize("script", [bad_unknown, bad_duplicate, bad_missing, bad_nan])
def test_invalid_output_degrades_to_fused_order(
    script: object,
) -> None:
    store = corpus(6)
    fused = run(store, None)

    result = run(store, ScriptedReranker(script))  # pyright: ignore[reportArgumentType]

    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.INVALID_OUTPUT
    assert order(result) == order(fused)


def test_reranker_error_degrades_and_query_continues() -> None:
    store = corpus(4)

    result = run(store, ScriptedReranker(failing_script))

    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.ERROR
    assert order(result) == order(run(store, None))


def test_reranker_timeout_degrades_without_waiting_for_the_call() -> None:
    release = Event()

    def blocked(request: RerankRequest) -> RerankOutput:
        _ = release.wait()
        return reverse_script(request)

    policy = POLICY.model_copy(
        update={"rerank": POLICY.rerank.model_copy(update={"timeout_seconds": 0.05})}
    )
    store = corpus(4)
    try:
        result = service(
            store, embeddings=None, reranker=ScriptedReranker(blocked), policy=policy
        ).execute(briefing("harbor crane"), CALLER, control(), LEXICAL)
    finally:
        release.set()

    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.TIMEOUT
    assert store.calls[-1] == "current_articles"


def test_rerank_slice_never_exceeds_remaining_deadline() -> None:
    clock = FakeMonotonic()
    ctl = QueryControl(deadline_seconds=0.2, monotonic=clock)
    scripted = ScriptedReranker(reverse_script)

    _ = run(corpus(3), scripted, ctl)

    assert math.isclose(scripted.requests[0].deadline_seconds, 0.2)


def test_cancel_during_rerank_stops_query_instead_of_degrading() -> None:
    ctl = control()
    store = corpus(4)

    def cancel_then_answer(request: RerankRequest) -> RerankOutput:
        ctl.cancel()
        return reverse_script(request)

    with pytest.raises(RetrievalCancelledError):
        _ = run(store, ScriptedReranker(cancel_then_answer), ctl)
    assert "current_articles" not in store.calls


def test_cancel_wakes_a_blocked_reranker_and_returns_no_evidence() -> None:
    ctl = control()
    store = corpus(4)
    release = Event()

    def cancel_and_block(request: RerankRequest) -> RerankOutput:
        ctl.cancel()
        _ = release.wait()
        return reverse_script(request)

    try:
        with pytest.raises(RetrievalCancelledError):
            _ = run(store, ScriptedReranker(cancel_and_block), ctl)
    finally:
        release.set()
    assert "current_articles" not in store.calls


def test_deadline_expiry_during_rerank_raises_typed_deadline() -> None:
    clock = FakeMonotonic()
    ctl = QueryControl(deadline_seconds=1.0, monotonic=clock)
    store = corpus(4)

    def exhaust(request: RerankRequest) -> RerankOutput:
        clock.now += 5.0
        return reverse_script(request)

    with pytest.raises(RetrievalDeadlineExceededError):
        _ = run(store, ScriptedReranker(exhaust), ctl)
    assert "current_articles" not in store.calls


def test_access_revoked_during_rerank_is_denied_at_final_boundary() -> None:
    access = MutableAccess()
    store = corpus(4)

    def revoke(request: RerankRequest) -> RerankOutput:
        access.revoked = True
        return reverse_script(request)

    with pytest.raises(RetrievalAccessDeniedError):
        _ = run(store, ScriptedReranker(revoke), access=access)
    assert "current_articles" not in store.calls
