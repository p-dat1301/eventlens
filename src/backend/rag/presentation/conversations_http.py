"""Public chat API: sessions, conversations and durable runs (``/api/v1``).

Identity is the server-side session only: an ``eventlens_session`` cookie
(HttpOnly, SameSite=Lax) or ``Authorization: Bearer <session token>``.
Headers such as ``X-User-ID`` and any owner/caller field in bodies are never
read. A foreign conversation or run answers ``404`` exactly like a missing
one. ``POST .../runs`` returns ``202`` with a ``status_url`` to poll; the run
keeps going when the client disconnects.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Final, Literal
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from backend.rag.application.use_cases.conversations import (
    ConversationPage,
    Conversations,
    Sessions,
)
from backend.rag.application.use_cases.inspect_source import (
    SourceAccessDeniedError,
    SourceStaleError,
)
from backend.rag.domain.catalog import (
    MAX_IDS_PER_FILTER,
    CatalogRequestError,
    CatalogStorageError,
    catalog_filters,
)
from backend.rag.domain.conversation import (
    MAX_QUESTION_CHARS,
    MAX_SCOPE_ARTICLES,
    MAX_TITLE_CHARS,
    ArticlePin,
    Conversation,
    ConversationError,
    ConversationScope,
    Message,
    QueryRun,
    ScopeType,
    SessionPrincipal,
    StoredCitation,
    conversation_scope,
)
from backend.rag.domain.retrieval import RetrievalStorageError
from backend.rag.presentation.api_errors import ApiError

COOKIE_NAME: Final = "eventlens_session"
API: Final = "/api/v1"


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


VersionId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,80}$")]


class ScopeFiltersIn(_Strict):
    """Catalog filter definition (same semantics as ``GET /api/v1/articles``)."""

    q: Annotated[str | None, StringConstraints(max_length=200)] = None
    topic_ids: Annotated[tuple[Id, ...], Field(max_length=MAX_IDS_PER_FILTER)] = ()
    location_ids: Annotated[tuple[Id, ...], Field(max_length=MAX_IDS_PER_FILTER)] = ()
    source_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_IDS_PER_FILTER)] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None
    include_descendants: bool = True


class ArticlePinIn(_Strict):
    """One article of an ``articles`` scope."""

    article_id: UUID
    article_version_id: VersionId | None = None


class ScopeIn(_Strict):
    """Conversation scope definition."""

    type: Literal["all", "filtered", "articles"]
    filters: ScopeFiltersIn | None = None
    articles: Annotated[
        tuple[ArticlePinIn, ...], Field(max_length=MAX_SCOPE_ARTICLES)
    ] = ()


class CreateConversationIn(_Strict):
    """``POST /api/v1/conversations``."""

    scope: ScopeIn
    title: Annotated[str | None, StringConstraints(max_length=MAX_TITLE_CHARS)] = None


class SubmitRunIn(_Strict):
    """``POST /api/v1/conversations/{id}/runs``."""

    question: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARS)
    ]


class SessionOut(_Strict):
    """New session; the token is shown once."""

    schema_version: Literal["session.v1"]
    session_token: str
    token_type: Literal["bearer"]
    cookie_name: str
    expires_at: datetime


class ConversationOut(_Strict):
    """Conversation header with its frozen scope definition."""

    schema_version: Literal["conversation.v1"]
    conversation_id: UUID
    title: str | None
    scope: dict[str, object]
    scope_sha256: str
    created_at: datetime
    updated_at: datetime


class ConversationListOut(_Strict):
    """Owner's conversations, newest first."""

    schema_version: Literal["conversation_list.v1"]
    items: tuple[ConversationOut, ...]
    next_cursor: str | None


class MessageOut(_Strict):
    """One message; assistant content is the validated answer text."""

    message_id: UUID
    run_id: UUID
    role: Literal["user", "assistant"]
    sequence: int
    content: str
    created_at: datetime


class MessagesOut(_Strict):
    """History page."""

    schema_version: Literal["message_list.v1"]
    conversation: ConversationOut
    items: tuple[MessageOut, ...]
    next_after_sequence: int | None


class RunAcceptedOut(_Strict):
    """202 body of a submitted (or replayed) run."""

    schema_version: Literal["run_accepted.v1"]
    run_id: UUID
    conversation_id: UUID
    status: str
    status_url: str
    replayed: bool


class RunErrorOut(_Strict):
    """Stable failure code of a failed run."""

    code: str
    reason: str | None


class CitedSpanOut(_Strict):
    """Cited span of the exact revision used."""

    start_offset: int
    end_offset: int
    excerpt: str


class CitationOut(_Strict):
    """Persisted citation identity; open it through ``source_ref_url``."""

    citation_id: UUID
    claim_id: UUID
    article_id: UUID
    article_version_id: str
    span: CitedSpanOut
    source_title: str
    source_name: str
    source_domain: str
    source_url: str
    source_published_at: datetime | None
    source_ref_url: str


class ClaimOut(_Strict):
    """Grounded claim."""

    claim_id: UUID
    text: str
    temporal_claim: dict[str, object] | None


class UncertaintyOut(_Strict):
    """Server-templated uncertainty."""

    uncertainty_id: UUID
    category: str
    explanation: str
    claim_id: UUID | None


class AnswerOut(_Strict):
    """Validated answer (no prompt, raw output or graph state)."""

    answer: str
    claims: tuple[ClaimOut, ...]
    uncertainties: tuple[UncertaintyOut, ...]
    retrieved_at: datetime
    model_version: str
    mode: str
    retrieval_mode: str
    audit: dict[str, str]


class RunOut(_Strict):
    """Run state; ``outcome`` only when ``status == completed``."""

    schema_version: Literal["query_run.v1"]
    run_id: UUID
    conversation_id: UUID
    sequence: int
    question: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    outcome: Literal["answered", "abstained"] | None
    abstention_reason: str | None
    error: RunErrorOut | None
    answer: AnswerOut | None
    citations: tuple[CitationOut, ...]
    effective_scope: dict[str, object] | None
    as_of: datetime | None
    source_versions: dict[str, str] | None
    usage: dict[str, object] | None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SourceRefOut(_Strict):
    """Fresh opaque reference for ``GET /briefing/sources/{source_ref}``."""

    schema_version: Literal["source_ref.v1"]
    source_ref: str
    viewer_url: str


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def conversation_out(item: Conversation) -> ConversationOut:
    """Domain -> DTO."""
    return ConversationOut(
        schema_version="conversation.v1",
        conversation_id=item.conversation_id,
        title=item.title,
        scope=item.scope,
        scope_sha256=item.scope_sha256,
        created_at=item.created_at.astimezone(UTC),
        updated_at=item.updated_at.astimezone(UTC),
    )


def _citation_out(run_id: UUID, c: StoredCitation) -> CitationOut:
    return CitationOut(
        citation_id=c.citation_id,
        claim_id=c.claim_id,
        article_id=c.article_id,
        article_version_id=c.content_hash,
        span=CitedSpanOut(
            start_offset=c.start_offset, end_offset=c.end_offset, excerpt=c.excerpt
        ),
        source_title=c.source_title,
        source_name=c.source_name,
        source_domain=c.source_domain,
        source_url=c.source_url,
        source_published_at=_utc(c.source_published_at),
        source_ref_url=f"{API}/runs/{run_id}/citations/{c.citation_id}/source-ref",
    )


def run_out(run: QueryRun) -> RunOut:
    """Domain -> DTO; a stored answer that fails validation is withheld."""
    answer = None
    if run.answer is not None:
        try:
            answer = AnswerOut.model_validate(run.answer)
        except ValidationError:
            answer = None
    return RunOut(
        schema_version="query_run.v1",
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        sequence=run.sequence,
        question=run.question,
        status=run.status.value,
        outcome=None if run.outcome is None else run.outcome.value,
        abstention_reason=run.abstention_reason,
        error=None
        if run.failure_code is None
        else RunErrorOut(code=run.failure_code, reason=run.failure_reason),
        answer=answer,
        citations=tuple(_citation_out(run.run_id, c) for c in run.citations),
        effective_scope=run.effective_scope,
        as_of=_utc(run.as_of),
        source_versions=run.source_versions,
        usage=run.usage,
        cancel_requested=run.cancel_requested,
        created_at=run.created_at.astimezone(UTC),
        started_at=_utc(run.started_at),
        finished_at=_utc(run.finished_at),
    )


def _message_out(m: Message) -> MessageOut:
    role: Literal["user", "assistant"] = "user" if m.role == "user" else "assistant"
    return MessageOut(
        message_id=m.message_id,
        run_id=m.run_id,
        role=role,
        sequence=m.sequence,
        content=m.content,
        created_at=m.created_at.astimezone(UTC),
    )


def scope_of(body: ScopeIn) -> ConversationScope:
    """Validate a scope DTO with the catalog filter rules."""
    try:
        filters = (
            None
            if body.filters is None
            else catalog_filters(
                q=body.filters.q,
                topic_ids=body.filters.topic_ids,
                location_ids=body.filters.location_ids,
                source_ids=body.filters.source_ids,
                published_after=body.filters.published_after,
                published_before=body.filters.published_before,
                include_descendants=body.filters.include_descendants,
            )
        )
    except CatalogRequestError as error:
        raise ApiError(400, "invalid_scope", error.code) from None
    return conversation_scope(
        ScopeType(body.type),
        filters,
        tuple(ArticlePin(a.article_id, a.article_version_id) for a in body.articles),
    )


async def call_mapped[T](call: Callable[[], T]) -> T:
    """Blocking use case off the loop; typed errors -> ``ApiError``."""
    try:
        return await anyio.to_thread.run_sync(call)
    except ConversationError as error:
        raise ApiError(error.status, error.code, error.reason) from None
    except SourceStaleError:
        raise ApiError(410, "source_stale") from None
    except SourceAccessDeniedError:
        raise ApiError(403, "access_denied") from None
    except CatalogRequestError as error:
        raise ApiError(400, "invalid_scope", error.code) from None
    except (RetrievalStorageError, CatalogStorageError):
        raise ApiError(503, "retrieval_unavailable") from None


def session_token(request: Request) -> str | None:
    """Session token from the cookie or a bearer header (nothing else)."""
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.cookies.get(COOKIE_NAME)


@dataclass(frozen=True, slots=True)
class ChatServices:
    """Composed chat use cases; ``None`` when storage is not configured."""

    sessions: Sessions | None
    conversations: Conversations | None
    secure_cookie: bool = False


def build_conversation_router(services: ChatServices) -> APIRouter:  # noqa: C901 - route table
    """Session, conversation and run routes."""
    router = APIRouter(prefix=API)

    def sessions() -> Sessions:
        if services.sessions is None:
            raise ApiError(503, "conversation_storage_unavailable", "not_configured")
        return services.sessions

    def chat() -> Conversations:
        if services.conversations is None:
            raise ApiError(503, "conversation_storage_unavailable", "not_configured")
        return services.conversations

    async def principal(request: Request) -> SessionPrincipal:
        token = session_token(request)
        store = sessions()
        return await call_mapped(lambda: store.resolve(token))

    @router.post("/sessions", status_code=201)
    async def create_session(response: Response) -> SessionOut:
        issued = await call_mapped(sessions().create)
        response.set_cookie(
            COOKIE_NAME,
            issued.token,
            httponly=True,
            samesite="lax",
            secure=services.secure_cookie,
            expires=issued.expires_at.astimezone(UTC),
            path="/",
        )
        return SessionOut(
            schema_version="session.v1",
            session_token=issued.token,
            token_type="bearer",  # noqa: S106 - token scheme, not a secret
            cookie_name=COOKIE_NAME,
            expires_at=issued.expires_at.astimezone(UTC),
        )

    @router.delete("/sessions/current", status_code=204)
    async def delete_session(request: Request, response: Response) -> None:
        token = session_token(request)
        store = sessions()
        if token:
            await call_mapped(lambda: store.revoke(token))
        response.delete_cookie(COOKIE_NAME, path="/")

    @router.post("/conversations", status_code=201)
    async def create_conversation(
        body: CreateConversationIn,
        owner: Annotated[SessionPrincipal, Depends(principal)],
    ) -> ConversationOut:
        use_case = chat()
        scope = await call_mapped(lambda: scope_of(body.scope))
        created = await call_mapped(lambda: use_case.create(owner, scope, body.title))
        return conversation_out(created)

    @router.get("/conversations")
    async def list_conversations(
        owner: Annotated[SessionPrincipal, Depends(principal)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ) -> ConversationListOut:
        use_case = chat()
        page: ConversationPage = await call_mapped(
            lambda: use_case.list(owner, limit, cursor)
        )
        return ConversationListOut(
            schema_version="conversation_list.v1",
            items=tuple(conversation_out(c) for c in page.items),
            next_cursor=page.next_cursor,
        )

    @router.get("/conversations/{conversation_id}/messages")
    async def messages(
        conversation_id: Annotated[UUID, Path()],
        owner: Annotated[SessionPrincipal, Depends(principal)],
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> MessagesOut:
        use_case = chat()
        conversation, items = await call_mapped(
            lambda: use_case.messages(owner, conversation_id, after_sequence, limit)
        )
        return MessagesOut(
            schema_version="message_list.v1",
            conversation=conversation_out(conversation),
            items=tuple(_message_out(m) for m in items),
            next_after_sequence=items[-1].sequence if len(items) == limit else None,
        )

    @router.post("/conversations/{conversation_id}/runs", status_code=202)
    async def submit_run(
        conversation_id: Annotated[UUID, Path()],
        body: SubmitRunIn,
        owner: Annotated[SessionPrincipal, Depends(principal)],
        response: Response,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=200)
        ] = None,
    ) -> RunAcceptedOut:
        use_case = chat()
        result = await call_mapped(
            lambda: use_case.submit(
                owner, conversation_id, body.question, idempotency_key
            )
        )
        run = result.run
        status_url = f"{API}/runs/{run.run_id}"
        response.headers["Location"] = status_url
        if not result.created:
            response.headers["Idempotent-Replayed"] = "true"
        return RunAcceptedOut(
            schema_version="run_accepted.v1",
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            status=run.status.value,
            status_url=status_url,
            replayed=not result.created,
        )

    @router.get("/runs/{run_id}")
    async def get_run(
        run_id: Annotated[UUID, Path()],
        owner: Annotated[SessionPrincipal, Depends(principal)],
    ) -> RunOut:
        use_case = chat()
        return run_out(await call_mapped(lambda: use_case.run(owner, run_id)))

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: Annotated[UUID, Path()],
        owner: Annotated[SessionPrincipal, Depends(principal)],
    ) -> RunOut:
        use_case = chat()
        return run_out(await call_mapped(lambda: use_case.cancel(owner, run_id)))

    @router.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: Annotated[UUID, Path()],
        owner: Annotated[SessionPrincipal, Depends(principal)],
    ) -> None:
        use_case = chat()
        await call_mapped(lambda: use_case.delete(owner, conversation_id))

    @router.post("/runs/{run_id}/citations/{citation_id}/source-ref", status_code=201)
    async def source_ref(
        run_id: Annotated[UUID, Path()],
        citation_id: Annotated[UUID, Path()],
        owner: Annotated[SessionPrincipal, Depends(principal)],
    ) -> SourceRefOut:
        use_case = chat()
        issued = await call_mapped(
            lambda: use_case.source_ref(owner, run_id, citation_id)
        )
        return SourceRefOut(
            schema_version="source_ref.v1",
            source_ref=issued.source_ref,
            viewer_url=f"/briefing/sources/{issued.source_ref}",
        )

    return router
