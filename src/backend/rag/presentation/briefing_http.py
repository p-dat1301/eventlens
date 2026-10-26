"""FastAPI adapter for the briefing and source-viewer use cases (T7).

Thin by design: strict request DTO -> ``BriefingQuery`` -> use case (run in a
worker thread, cancelled through ``QueryControl`` when the client goes away)
-> allowlisted public DTO. Typed errors map to stable codes; error bodies carry
codes only, never provider bodies, prompts, article text or storage URLs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, ClassVar, Literal, Protocol
from uuid import UUID

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from backend.rag.application.use_cases.briefing import (
    BriefingConfigurationError,
    BriefingOutcome,
    BriefingUseCase,
    BriefingWorkflowError,
)
from backend.rag.application.use_cases.inspect_source import (
    MAX_REFERENCE_LENGTH,
    InspectSource,
    SourceAccessDeniedError,
    SourceError,
    SourceNotFoundError,
    SourceStaleError,
    SourceView,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalConfigurationError,
    RetrievalDeadlineExceededError,
    RetrievalError,
    RetrievalStorageError,
)
from backend.rag.domain.synthesis_errors import (
    ProviderActivationError,
    SynthesisAccessDeniedError,
    SynthesisBudgetError,
    SynthesisCancelledError,
    SynthesisConcurrencyRejectedError,
    SynthesisConfigurationError,
    SynthesisDeadlineExceededError,
    SynthesisError,
    SynthesisEvidenceStaleError,
    SynthesisGroundingError,
    SynthesisMalformedOutputError,
    SynthesisProviderError,
    SynthesisStorageError,
)
from backend.rag.domain.temporal import TemporalClaim
from backend.shared.contracts.taxonomy import TopicTag

MAX_QUERY_CHARS = 2_000
REQUEST_SCHEMA = "briefing_request.v1"
RESPONSE_SCHEMA = "briefing_response.v1"
SOURCE_SCHEMA = "briefing_source.v1"
ERROR_SCHEMA = "briefing_error.v1"

QueryText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_QUERY_CHARS, pattern=r"\S"),
]
Domain = Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]+$", max_length=253)]


class InvalidQueryError(ValueError):
    """Request passed the DTO but violates the query contract (fixed message)."""

    code: ClassVar[str] = "invalid_query"

    def __init__(self) -> None:
        """Fixed message; validation details are never echoed."""
        super().__init__("briefing query rejected")


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class BriefingRequest(_Strict):
    """Strict public request; caller identity and policy are server-owned."""

    schema_version: Literal["briefing_request.v1"]
    query: QueryText
    published_after: datetime | None = None
    published_before: datetime | None = None
    publication_time_basis: Literal["source_published_at"] = "source_published_at"
    source_domains: Annotated[tuple[Domain, ...], Field(max_length=20)] = ()
    topic_filters: Annotated[tuple[TopicTag, ...], Field(max_length=20)] = ()
    entity_ids: Annotated[tuple[UUID, ...], Field(max_length=20)] = ()
    context_budget: Annotated[int, Field(strict=True, ge=1, le=12_000)] = 12_000
    result_limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5

    def to_query(self) -> BriefingQuery:
        """Validate into the RAG query contract (``InvalidQueryError``)."""
        try:
            return self._query()
        except ValidationError:
            raise InvalidQueryError from None

    def _query(self) -> BriefingQuery:
        return BriefingQuery.model_validate(
            {
                "schema_version": "briefing_query.v1",
                "query": self.query,
                "source_domains": self.source_domains,
                "topic_filters": self.topic_filters,
                "entity_ids": self.entity_ids,
                "published_after": self.published_after,
                "published_before": self.published_before,
                "publication_time_basis": self.publication_time_basis,
                "result_limit": self.result_limit,
                "context_budget": self.context_budget,
            }
        )


class PublicClaim(_Strict):
    """One grounded claim."""

    claim_id: UUID
    text: str
    temporal_claim: TemporalClaim | None


class PublicCitedSpan(_Strict):
    """Cited span of the revision the answer was grounded on."""

    start_offset: int
    end_offset: int
    excerpt: str


class PublicCitation(_Strict):
    """Citation with an opaque source reference for the viewer."""

    citation_id: UUID
    claim_id: UUID
    source_ref: str
    source_title: str
    source_name: str
    source_domain: str
    source_url: str
    source_published_at: datetime | None
    span: PublicCitedSpan


class PublicUncertainty(_Strict):
    """Server-templated uncertainty."""

    uncertainty_id: UUID
    category: str
    explanation: str
    claim_id: UUID | None


class AuditMetadata(_Strict):
    """Versions and counts that make one answer reproducible (no content)."""

    retrieval_policy_id: str
    fusion_policy_id: str
    index_version: str
    corpus_generation: str
    chunker_version: str
    embedding_model: str
    embedding_version: str
    reranker_id: str | None
    vector_branch_status: str
    rerank_status: str
    coverage_status: str
    synthesis_policy_id: str
    activation_policy_id: str
    mode: Literal["fixture", "live"]
    prompt_version: str
    draft_schema_version: str
    template_version: str
    tokenizer_id: str
    tokenizer_version: str
    model_calls: int
    input_tokens_charged: int
    output_tokens_charged: int
    usage_status: str


class BriefingResponse(_Strict):
    """Public briefing answer; no prompt, raw output, graph state or lineage."""

    schema_version: Literal["briefing_response.v1"]
    outcome: Literal["answered", "abstained_no_evidence", "abstained_by_model"]
    abstention_reason: str | None
    answer: str
    claims: tuple[PublicClaim, ...]
    citations: tuple[PublicCitation, ...]
    uncertainties: tuple[PublicUncertainty, ...]
    retrieved_at: datetime
    model_version: str
    retrieval_mode: str
    query_time_basis: Literal["source_published_at"]
    audit: AuditMetadata


class SourceResponse(_Strict):
    """Authorized current source and cited span."""

    schema_version: Literal["briefing_source.v1"]
    title: str
    source_name: str
    source_domain: str
    source_url: str
    source_published_at: datetime | None
    normalized_content: str
    span: PublicCitedSpan


class ErrorDetail(_Strict):
    """Stable error code plus an optional enum reason."""

    code: str
    reason: str | None = None


class ErrorResponse(_Strict):
    """Error envelope."""

    schema_version: Literal["briefing_error.v1"]
    error: ErrorDetail


def to_response(outcome: BriefingOutcome) -> BriefingResponse:
    """Map the use-case result to the public allowlist."""
    result = outcome.result
    retrieval = outcome.retrieval
    answer = result.answer
    usage = result.usage
    provenance = result.provenance
    rprov = retrieval.provenance
    return BriefingResponse(
        schema_version=RESPONSE_SCHEMA,
        outcome=result.outcome.value,
        abstention_reason=result.abstention_reason,
        answer=answer.answer,
        claims=tuple(
            PublicClaim(
                claim_id=c.claim_id, text=c.text, temporal_claim=c.temporal_claim
            )
            for c in answer.claims
        ),
        citations=tuple(
            PublicCitation(
                citation_id=s.citation.citation_id,
                claim_id=s.citation.claim_id,
                source_ref=s.source_ref,
                source_title=s.citation.source_title,
                source_name=s.source_name,
                source_domain=s.source_domain,
                source_url=str(s.citation.source_reference),
                source_published_at=s.source_published_at,
                span=PublicCitedSpan(
                    start_offset=s.span.start_offset,
                    end_offset=s.span.end_offset,
                    excerpt=s.span.excerpt,
                ),
            )
            for s in outcome.cited_sources
        ),
        uncertainties=tuple(
            PublicUncertainty(
                uncertainty_id=u.uncertainty_id,
                category=u.category,
                explanation=u.explanation,
                claim_id=u.claim_id,
            )
            for u in answer.uncertainties
        ),
        retrieved_at=answer.retrieved_at,
        model_version=answer.model_version,
        retrieval_mode=retrieval.retrieval_mode.value,
        query_time_basis=rprov.publication_time_basis,
        audit=AuditMetadata(
            retrieval_policy_id=rprov.policy_id,
            fusion_policy_id=rprov.fusion_policy_id,
            index_version=answer.index_version,
            corpus_generation=rprov.corpus_generation,
            chunker_version=rprov.chunker_version,
            embedding_model=rprov.embedding_model,
            embedding_version=rprov.embedding_version,
            reranker_id=rprov.reranker_id,
            vector_branch_status=retrieval.vector_branch_status.value,
            rerank_status=retrieval.rerank_status.value,
            coverage_status=result.coverage.status.value,
            synthesis_policy_id=provenance.synthesis_policy_id,
            activation_policy_id=provenance.activation_policy_id,
            mode=provenance.mode,
            prompt_version=provenance.prompt_version,
            draft_schema_version=provenance.draft_schema_version,
            template_version=provenance.template_version,
            tokenizer_id=provenance.tokenizer.tokenizer_id,
            tokenizer_version=provenance.tokenizer.version,
            model_calls=usage.model_calls,
            input_tokens_charged=usage.input_tokens_charged,
            output_tokens_charged=usage.output_tokens_charged,
            usage_status=usage.usage_status.value,
        ),
    )


def to_source_response(view: SourceView) -> SourceResponse:
    """Map an authorized source view to the public allowlist."""
    return SourceResponse(
        schema_version=SOURCE_SCHEMA,
        title=view.title,
        source_name=view.source_name,
        source_domain=view.source_domain,
        source_url=view.source_url,
        source_published_at=view.source_published_at,
        normalized_content=view.normalized_content,
        span=PublicCitedSpan(
            start_offset=view.span.start_offset,
            end_offset=view.span.end_offset,
            excerpt=view.span.excerpt,
        ),
    )


class CallerUnauthenticatedError(RuntimeError):
    """The resolver could not establish a caller (fixed message)."""

    code: ClassVar[str] = "unauthenticated"

    def __init__(self) -> None:
        """Fixed message; credentials are never echoed."""
        super().__init__("caller not authenticated")


# Stable error table: (status, public code). First matching class wins.
ERROR_TABLE: tuple[tuple[type[Exception], int, str], ...] = (
    (RequestValidationError, 400, "invalid_request"),
    (InvalidQueryError, 400, "invalid_query"),
    (CallerUnauthenticatedError, 401, "unauthenticated"),
    (RetrievalAccessDeniedError, 403, "access_denied"),
    (SynthesisAccessDeniedError, 403, "access_denied"),
    (SourceAccessDeniedError, 403, "access_denied"),
    (SourceNotFoundError, 404, "source_not_found"),
    (SourceStaleError, 410, "source_stale"),
    (RetrievalCancelledError, 408, "request_cancelled"),
    (SynthesisCancelledError, 408, "request_cancelled"),
    (RetrievalDeadlineExceededError, 408, "deadline_exceeded"),
    (SynthesisDeadlineExceededError, 408, "deadline_exceeded"),
    (SynthesisEvidenceStaleError, 409, "evidence_stale"),
    (SynthesisBudgetError, 422, "budget_exceeded"),
    (SynthesisConcurrencyRejectedError, 429, "capacity_exceeded"),
    (SynthesisMalformedOutputError, 502, "invalid_grounded_answer"),
    (SynthesisGroundingError, 502, "invalid_grounded_answer"),
    (RetrievalStorageError, 503, "retrieval_unavailable"),
    (SynthesisStorageError, 503, "retrieval_unavailable"),
    (SynthesisProviderError, 503, "model_unavailable"),
    (RetrievalConfigurationError, 500, "configuration_invalid"),
    (SynthesisConfigurationError, 500, "configuration_invalid"),
    (ProviderActivationError, 500, "configuration_invalid"),
    (BriefingConfigurationError, 500, "configuration_invalid"),
    (BriefingWorkflowError, 500, "internal_error"),
    (RetrievalError, 500, "internal_error"),
    (SynthesisError, 500, "internal_error"),
    (SourceError, 500, "internal_error"),
)
HANDLED: tuple[type[Exception], ...] = tuple(
    dict.fromkeys(e for e, _, _ in ERROR_TABLE)
)


def error_status(error: Exception) -> tuple[int, ErrorDetail]:
    """Stable status and code for ``error``; never echoes its message."""
    for kind, status, code in ERROR_TABLE:
        if isinstance(error, kind):
            reason: object = getattr(error, "reason", None)
            return status, ErrorDetail(
                code=code, reason=None if reason is None else str(reason)
            )
    return 500, ErrorDetail(code="internal_error")


def _error_response(_: Request, error: Exception) -> JSONResponse:
    status, detail = error_status(error)
    body = ErrorResponse(schema_version=ERROR_SCHEMA, error=detail)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def install_error_handlers(app: FastAPI) -> None:
    """Register the stable error mapping on ``app``."""
    for kind in HANDLED:
        app.add_exception_handler(kind, _error_response)


async def run_cancellable[T](call: Callable[[], T], cancel: Callable[[], None]) -> T:
    """Run blocking work off the loop; cancel it cooperatively on disconnect."""
    try:
        return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
    except anyio.get_cancelled_exc_class():
        cancel()
        raise


class CallerResolver(Protocol):
    """Server-side caller resolution; the request body is never consulted.

    A real authenticator plugs in here and raises
    ``CallerUnauthenticatedError`` when it cannot establish a caller.
    """

    def resolve(self, request: Request) -> CallerScope:
        """Caller scope for ``request``."""
        ...


@dataclass(frozen=True, slots=True)
class FixedCallerResolver:
    """Fixture resolver: every request is the composed fixture caller."""

    caller: CallerScope

    def resolve(self, request: Request) -> CallerScope:
        """Ignore the request; return the composed caller."""
        del request
        return self.caller


def build_briefing_router(
    use_case: BriefingUseCase, sources: InspectSource, callers: CallerResolver
) -> APIRouter:
    """Router over composed use cases; no SQL, provider or graph code here."""
    router = APIRouter()

    def caller_of(request: Request) -> CallerScope:
        return callers.resolve(request)

    @router.post("/briefing", responses={400: {"model": ErrorResponse}})
    async def briefing(
        body: BriefingRequest, caller: Annotated[CallerScope, Depends(caller_of)]
    ) -> BriefingResponse:
        query = body.to_query()
        control = use_case.new_control()
        outcome = await run_cancellable(
            lambda: use_case.execute(query, caller=caller, control=control),
            control.cancel,
        )
        return to_response(outcome)

    @router.get("/briefing/sources/{source_ref}")
    async def source(
        source_ref: Annotated[str, Path(max_length=MAX_REFERENCE_LENGTH)],
        caller: Annotated[CallerScope, Depends(caller_of)],
    ) -> SourceResponse:
        view = await run_cancellable(
            lambda: sources.execute(source_ref, caller), lambda: None
        )
        return to_source_response(view)

    return router
