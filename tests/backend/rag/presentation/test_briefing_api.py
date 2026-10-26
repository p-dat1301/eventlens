"""T7 HTTP adapter over ASGI: full fake path through the compiled graph."""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from uuid import UUID

import anyio
import anyio.from_thread
import httpx2
import pytest
from fastapi import Request

from backend.rag.api_main import create_app
from backend.rag.application.ports.briefing import BriefingInvocation, BriefingRun
from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.composition import FIXTURE_CALLER
from backend.rag.domain.access import CallerScope
from backend.rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisProviderError,
)
from backend.rag.presentation.briefing_http import (
    MAX_QUERY_CHARS,
    CallerUnauthenticatedError,
    run_cancellable,
)
from backend.rag.workflows.query.workflow import BriefingGraph
from briefing_support import (
    NO_MATCH,
    QUESTION,
    Harness,
    answer_of,
    body,
    draft_citing,
    error_of,
    harness,
    json_keys,
    raising,
    returning,
    revise,
)

FORBIDDEN_KEYS = {
    "source_documents",
    "content_hash",
    "content_fingerprint",
    "raw_content",
    "normalized_content",
    "prompt",
    "messages",
    "instructions",
    "article_id",
    "evidence_span_id",
    "storage_uri",
    "database_url",
    "api_key",
    "state",
    "reasoning",
}
UNKNOWN_SPAN = UUID("00000000-0000-4000-8000-00000000dead")
pytestmark = pytest.mark.anyio


async def _post(h: Harness, payload: dict[str, object]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=create_app(h.runtime))
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=10.0
    ) as client:
        return await client.post("/briefing", json=payload)


@dataclass(slots=True)
class SpyWorkflow:
    """Count invocations of the real compiled graph."""

    graph: BriefingGraph
    calls: int = 0

    def run(self, invocation: BriefingInvocation) -> BriefingRun:
        """Delegate to the compiled graph."""
        self.calls += 1
        return self.graph.run(invocation)


async def test_valid_briefing_returns_public_answer() -> None:
    # Given
    h = harness()

    # When
    response = await _post(h, body())

    # Then
    assert response.status_code == 200
    answer = answer_of(response)
    assert answer.outcome == "answered"
    assert answer.claims
    claim_ids = {claim.claim_id for claim in answer.claims}
    for citation in answer.citations:
        assert citation.claim_id in claim_ids
        assert citation.source_ref
        assert citation.source_url.startswith("https://")
        assert citation.span.excerpt
    assert answer.model_version
    assert answer.retrieval_mode == "lexical_only"
    assert answer.audit.mode == "fixture"
    assert answer.audit.model_calls == 1
    assert json_keys(response.text) & FORBIDDEN_KEYS == set()
    assert h.telemetry.events[-1].route == "answered"


async def test_query_time_basis_is_source_published_at() -> None:
    # When
    answer = answer_of(await _post(harness(), body()))

    # Then
    assert answer.query_time_basis == "source_published_at"
    assert all(c.source_published_at for c in answer.citations)


async def test_full_asgi_path_runs_through_compiled_graph() -> None:
    # Given: the runtime's graph is the only workflow, compiled at composition.
    h = harness()
    spy = SpyWorkflow(h.runtime.graph)
    h.runtime = replace(h.runtime, use_case=replace(h.runtime.use_case, workflow=spy))

    # When
    first = await _post(h, body())
    second = await _post(h, body(NO_MATCH))

    # Then
    assert first.status_code == second.status_code == 200
    assert spy.calls == 2
    assert [e.route for e in h.telemetry.events] == [
        "answered",
        "abstained_no_evidence",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        body(caller_scope={"principal_id": "admin"}),
        body(access_policy="allow-all"),
        body(unexpected=True),
        {"query": QUESTION},
        body(schema_version="briefing_request.v2"),
        body(result_limit="5"),
        body(context_budget=0),
    ],
)
async def test_strict_request_rejects_bad_fields(payload: dict[str, object]) -> None:
    # Given
    h = harness()

    # When
    response = await _post(h, payload)

    # Then
    assert response.status_code == 400
    error = error_of(response)
    assert (error.code, error.reason) == ("invalid_request", None)
    assert h.model.calls == 0
    assert h.store.reads == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_empty_query_is_rejected(text: str) -> None:
    # When
    response = await _post(harness(), body(text))

    # Then
    assert response.status_code == 400
    assert error_of(response).code == "invalid_request"


async def test_too_long_query_is_rejected_without_echo() -> None:
    # Given
    text = "harbor " * (MAX_QUERY_CHARS // 7 + 1)

    # When
    response = await _post(harness(), body(text))

    # Then
    assert response.status_code == 400
    assert "harbor" not in response.text


async def test_invalid_publication_range_is_invalid_query() -> None:
    # When
    response = await _post(
        harness(),
        body(
            published_after="2026-03-05T00:00:00Z",
            published_before="2026-03-01T00:00:00Z",
        ),
    )

    # Then
    assert response.status_code == 400
    assert error_of(response).code == "invalid_query"


async def test_no_evidence_returns_explicit_abstention() -> None:
    # Given
    h = harness()

    # When
    response = await _post(h, body(NO_MATCH))

    # Then
    assert response.status_code == 200
    answer = answer_of(response)
    assert answer.outcome == "abstained_no_evidence"
    assert answer.abstention_reason == "no_matches"
    assert answer.claims == ()
    assert answer.citations == ()
    assert answer.uncertainties
    assert answer.audit.model_calls == 0
    assert h.model.calls == 0


async def test_publication_filters_reach_retrieval() -> None:
    # When: a range and domain with no fixture articles in it.
    response = await _post(
        harness(),
        body(
            published_after="2030-01-01T00:00:00Z",
            published_before="2030-02-01T00:00:00Z",
            source_domains=["fixture.example"],
        ),
    )

    # Then
    assert response.status_code == 200
    assert answer_of(response).outcome == "abstained_no_evidence"


def _storage_down(h: Harness) -> None:
    h.store.available = False


def _model(
    behaviour: Callable[[SynthesisRequest], SynthesisResponse],
) -> Callable[[Harness], None]:
    def setup(h: Harness) -> None:
        h.model.behaviour = behaviour

    return setup


def _stale(h: Harness) -> None:
    def behaviour(request: SynthesisRequest) -> SynthesisResponse:
        for document in tuple(h.store.documents.values()):
            h.store.add(revise(document))
        return h.model.fake(request)

    h.model.behaviour = behaviour


def _revoke(h: Harness) -> None:
    h.access.revoked = True


@pytest.mark.parametrize(
    ("setup", "status", "code", "reason"),
    [
        (_storage_down, 503, "retrieval_unavailable", None),
        (
            _model(raising(SynthesisProviderError(ProviderFailureCode.UNAVAILABLE))),
            503,
            "model_unavailable",
            "provider_unavailable",
        ),
        (
            _model(raising(SynthesisProviderError(ProviderFailureCode.TIMEOUT))),
            503,
            "model_unavailable",
            "provider_timeout",
        ),
        (
            _model(returning("not json {")),
            502,
            "invalid_grounded_answer",
            "invalid_json",
        ),
        (
            _model(returning(draft_citing(UNKNOWN_SPAN))),
            502,
            "invalid_grounded_answer",
            "unknown_span",
        ),
        (_stale, 409, "evidence_stale", None),
        (_revoke, 403, "access_denied", None),
    ],
    ids=[
        "storage",
        "provider_unavailable",
        "provider_timeout",
        "malformed",
        "invalid_citation",
        "stale_hash",
        "access",
    ],
)
async def test_typed_failures_map_to_stable_codes(
    setup: Callable[[Harness], None], status: int, code: str, reason: str | None
) -> None:
    # Given
    h = harness()
    setup(h)

    # When
    response = await _post(h, body())

    # Then
    assert response.status_code == status
    error = error_of(response)
    assert (error.code, error.reason) == (code, reason)
    for leak in ("Harbor", "sha256:", "postgresql://", "synthesis_draft", "East Pier"):
        assert leak not in response.text
    assert h.runtime.use_case.limiter.in_use == 0


async def test_budget_exceeded_maps_to_422_without_work() -> None:
    # Given
    h = harness()

    # When
    response = await _post(h, body(context_budget=8))

    # Then
    assert response.status_code == 422
    error = error_of(response)
    assert (error.code, error.reason) == ("budget_exceeded", "context_too_small")
    assert h.model.calls == 0
    assert h.store.reads == 0


async def test_deadline_maps_to_408_without_model_work() -> None:
    # Given: a clock that jumps past the 20 s deadline right after admission.
    ticks = iter([0.0, 0.0, 0.0, *([100.0] * 1000)])
    h = harness(monotonic=lambda: next(ticks))

    # When
    response = await _post(h, body())

    # Then
    assert response.status_code == 408
    assert error_of(response).code == "deadline_exceeded"
    assert h.model.calls == 0
    assert h.store.reads == 0


async def test_client_cancellation_cancels_the_query_control() -> None:
    # Given
    cancelled: list[bool] = []
    started = anyio.Event()

    def blocking() -> None:
        anyio.from_thread.run_sync(started.set)
        time.sleep(0.2)

    async def call() -> None:
        await run_cancellable(blocking, lambda: cancelled.append(True))

    # When
    async with anyio.create_task_group() as group:
        _ = group.start_soon(call)
        await started.wait()
        group.cancel_scope.cancel()

    # Then
    assert cancelled == [True]


async def test_health_endpoint_unchanged() -> None:
    # Given
    transport = httpx2.ASGITransport(app=create_app(harness().runtime))

    # When
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        response = await client.get("/health")

    # Then
    assert response.status_code == 200
    assert response.text == '{"status":"ok"}'


async def test_default_app_never_activates_live_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: provider-looking environment variables are present.
    monkeypatch.setenv("EVENTLENS_PROVIDER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    # When
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=10.0
    ) as client:
        response = await client.post("/briefing", json=body())

    # Then
    assert response.status_code == 200
    assert answer_of(response).audit.mode == "fixture"
    assert "sk-test" not in response.text


@dataclass(frozen=True, slots=True)
class RejectingResolver:
    """Stands in for an authenticator that cannot identify the caller."""

    def resolve(self, request: Request) -> CallerScope:
        del request
        raise CallerUnauthenticatedError


@dataclass(slots=True)
class RecordingResolver:
    """Resolver that records each request it saw."""

    seen: list[str]

    def resolve(self, request: Request) -> CallerScope:
        self.seen.append(request.url.path)
        return FIXTURE_CALLER


async def test_unauthenticated_caller_is_rejected_before_work() -> None:
    # Given
    h = harness()
    h.runtime = replace(h.runtime, callers=RejectingResolver())

    # When
    response = await _post(h, body())

    # Then
    assert response.status_code == 401
    assert error_of(response).code == "unauthenticated"
    assert h.model.calls == 0
    assert h.store.reads == 0


async def test_caller_comes_from_the_server_resolver() -> None:
    # Given
    h = harness()
    resolver = RecordingResolver([])
    h.runtime = replace(h.runtime, callers=resolver)

    # When
    response = await _post(h, body())

    # Then
    assert response.status_code == 200
    assert resolver.seen == ["/briefing"]
