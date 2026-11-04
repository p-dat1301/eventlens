"""``api_error.v1`` envelope and request ids for crud-api ``/api`` routes."""

import re
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.crud.application.ports.article_catalog import (
    CatalogContractError,
    CatalogUnavailableError,
    CatalogUpstreamError,
)
from backend.shared.contracts.catalog import ApiErrorDetail, ApiErrorV1

REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _body(request: Request, code: str, reason: str | None) -> dict[str, object]:
    rid = getattr(request.state, "request_id", None)
    return ApiErrorV1(
        schema_version="api_error.v1",
        error=ApiErrorDetail(
            code=code, reason=reason, request_id=rid if isinstance(rid, str) else None
        ),
    ).model_dump(mode="json")


def _respond(request: Request, status: int, code: str, reason: str | None) -> Response:
    headers = {"Retry-After": "5"} if status == 503 else None  # noqa: PLR2004
    return JSONResponse(
        status_code=status, content=_body(request, code, reason), headers=headers
    )


def install_errors(app: FastAPI) -> None:
    """Request ids plus typed catalog/validation error handlers."""

    @app.middleware("http")
    async def _with_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = (
            incoming if _REQUEST_ID.match(incoming) else uuid4().hex
        )
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = str(request.state.request_id)
        return response

    def upstream(request: Request, error: Exception) -> Response:
        assert isinstance(error, CatalogUpstreamError)  # noqa: S101
        return _respond(request, error.status, error.code, error.reason)

    def unavailable(request: Request, error: Exception) -> Response:
        assert isinstance(error, CatalogUnavailableError)  # noqa: S101
        return _respond(request, 503, error.code, error.reason)

    def contract(request: Request, error: Exception) -> Response:
        del error
        return _respond(request, 502, CatalogContractError.code, None)

    def validation(request: Request, error: Exception) -> Response:
        del error
        return _respond(request, 400, "invalid_request", None)

    async def http(request: Request, error: Exception) -> Response:
        assert isinstance(error, StarletteHTTPException)  # noqa: S101
        if not request.url.path.startswith("/api/"):
            return await http_exception_handler(request, error)
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error"
        )
        return _respond(request, error.status_code, code, None)

    app.add_exception_handler(CatalogUpstreamError, upstream)
    app.add_exception_handler(CatalogUnavailableError, unavailable)
    app.add_exception_handler(CatalogContractError, contract)
    app.add_exception_handler(RequestValidationError, validation)
    app.add_exception_handler(StarletteHTTPException, http)
