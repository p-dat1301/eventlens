"""Fixture-only helpers for T7 briefing tests (no network, no provider)."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx2
from pydantic import JsonValue, TypeAdapter

from backend.rag.application.ports.synthesis import (
    ConcurrencyLimiter,
    SynthesisModelPort,
    SynthesisRequest,
    SynthesisResponse,
)
from backend.rag.application.ports.telemetry import BriefingTelemetryEvent
from backend.rag.composition import (
    FIXTURE_CALLER,
    BriefingRuntime,
    compose_briefing,
    fixture_model_identity,
    fixture_store,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.synthesis import ChatModelIdentity, UsageStatus
from backend.rag.domain.synthesis_errors import SynthesisError
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from backend.rag.presentation.briefing_http import (
    BriefingResponse,
    ErrorDetail,
    ErrorResponse,
    SourceResponse,
)

QUESTION = "When did East Pier close and reopen?"
NO_MATCH = "zzzqqq xylophonequartz"
CLOCK = datetime(2026, 4, 1, tzinfo=UTC)
_OBJECT = TypeAdapter(dict[str, object])
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

type Behaviour = Callable[[SynthesisRequest], SynthesisResponse]


@dataclass(slots=True)
class MutableAccess:
    """Fixture policy that a test can revoke at any point."""

    revoked: bool = False

    def allows(self, scope: CallerScope) -> bool:
        """Fixture caller only, unless revoked."""
        return not self.revoked and scope == FIXTURE_CALLER


@dataclass(slots=True)
class CountingModel:
    """Count model calls; delegate to a behaviour or the deterministic fake."""

    behaviour: Behaviour | None = None
    calls: int = 0
    requests: list[SynthesisRequest] = field(default_factory=list[SynthesisRequest])
    _fake: DeterministicFakeSynthesizer = field(
        default_factory=lambda: DeterministicFakeSynthesizer(fixture_model_identity())
    )

    @property
    def identity(self) -> ChatModelIdentity:
        """Fixture chat identity."""
        return fixture_model_identity()

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Record the call and answer."""
        self.calls += 1
        self.requests.append(request)
        if self.behaviour is None:
            return self._fake.synthesize(request)
        return self.behaviour(request)

    def fake(self, request: SynthesisRequest) -> SynthesisResponse:
        """Deterministic grounded answer for ``request``."""
        return self._fake.synthesize(request)


@dataclass(slots=True)
class RecordingTelemetry:
    """Keep briefing telemetry events."""

    events: list[BriefingTelemetryEvent] = field(
        default_factory=list[BriefingTelemetryEvent]
    )

    def emit(self, event: BriefingTelemetryEvent) -> None:
        """Append."""
        self.events.append(event)


@dataclass(slots=True)
class Harness:
    """One composed runtime with handles on its fakes."""

    runtime: BriefingRuntime
    store: InMemoryRetrievalStore
    model: CountingModel
    access: MutableAccess
    telemetry: RecordingTelemetry


def harness(  # noqa: PLR0913
    *,
    behaviour: Behaviour | None = None,
    store: InMemoryRetrievalStore | None = None,
    vectors: bool = False,
    limiter: ConcurrencyLimiter | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    model: SynthesisModelPort | None = None,
) -> Harness:
    """Fixture runtime over the 30-article corpus (lexical-only by default)."""
    if store is None:
        store = fixture_store()
        if vectors:
            for document in tuple(store.documents.values()):
                store.add(document, vectors=True)
    counting = CountingModel(behaviour)
    access = MutableAccess()
    telemetry = RecordingTelemetry()
    runtime = compose_briefing(
        store,
        model=model or counting,
        access=access,
        limiter=limiter,
        telemetry=telemetry,
        clock=lambda: CLOCK,
        monotonic=monotonic,
    )
    return Harness(runtime, store, counting, access, telemetry)


def revise(document: ArticleDocument, suffix: str = " Corrected.") -> ArticleDocument:
    """New current revision of ``document`` (new content hash, no old spans)."""
    content = document.normalized_content + suffix
    digest = f"sha256:{sha256(content.encode()).hexdigest()}"
    payload = _OBJECT.validate_json(document.model_dump_json())
    payload.update(
        raw_content=content,
        normalized_content=content,
        content_hash=digest,
        content_fingerprint=digest,
        evidence_spans=[],
    )
    return ArticleDocument.model_validate_json(_OBJECT.dump_json(payload))


def answer_of(response: httpx2.Response) -> BriefingResponse:
    """Typed public answer."""
    return BriefingResponse.model_validate_json(response.text)


def source_of(response: httpx2.Response) -> SourceResponse:
    """Typed source view."""
    return SourceResponse.model_validate_json(response.text)


def error_of(response: httpx2.Response) -> ErrorDetail:
    """Typed error detail (envelope validated strictly)."""
    return ErrorResponse.model_validate_json(response.text).error


def json_keys(text: str) -> set[str]:
    """Every object key anywhere in a JSON document."""
    return _keys(_JSON.validate_json(text))


def _keys(value: JsonValue) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set[str]()
        for item in value:
            found |= _keys(item)
        return found
    return set()


def query(text: str = QUESTION, **overrides: object) -> BriefingQuery:
    """Server-side query contract."""
    payload: dict[str, object] = {
        "schema_version": "briefing_query.v1",
        "query": text,
        "publication_time_basis": "source_published_at",
        "result_limit": 5,
        "context_budget": 12_000,
    }
    payload.update(overrides)
    return BriefingQuery.model_validate(payload)


def body(text: str = QUESTION, **overrides: object) -> dict[str, object]:
    """Public HTTP request body."""
    payload: dict[str, object] = {
        "schema_version": "briefing_request.v1",
        "query": text,
    }
    payload.update(overrides)
    return payload


def respond(content: str) -> SynthesisResponse:
    """Scripted provider response without usage."""
    return SynthesisResponse(
        content=content,
        usage=None,
        usage_status=UsageStatus.MISSING,
        reported_model=fixture_model_identity().model_id,
    )


def draft_citing(span_id: UUID) -> str:
    """Strict draft citing one span ID."""
    return json.dumps(
        {
            "schema_version": "synthesis_draft.v1",
            "evidence_status": "sufficient",
            "claims": [
                {
                    "claim_ref": "c1",
                    "text": "An invented claim.",
                    "evidence_span_ids": [str(span_id)],
                    "temporal_claim": None,
                }
            ],
            "uncertainties": [],
        }
    )


def raising(error: SynthesisError) -> Behaviour:
    """Behaviour that raises a typed provider-side error."""

    def behaviour(request: SynthesisRequest) -> SynthesisResponse:
        del request
        raise error

    return behaviour


def returning(content: str) -> Behaviour:
    """Behaviour that returns fixed content."""

    def behaviour(request: SynthesisRequest) -> SynthesisResponse:
        del request
        return respond(content)

    return behaviour
