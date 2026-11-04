"""``api_error.v1`` envelope, request ids and handlers for ``/api`` routes.

Routers raise ``ApiError(status, code, reason)``; nothing from SQL, providers
or article text reaches the body. Request validation errors keep the legacy
``briefing_error.v1`` envelope on ``/briefing*`` and use ``api_error.v1`` on
``/api/*`` and ``/internal/*``.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.shared.contracts.catalog import ApiErrorDetail, ApiErrorV1

REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9-]{8,64}$")
API_PREFIXES: Final = ("/api/", "/internal/")


class ApiError(Exception):
    """Typed HTTP failure with a stable public code."""

    def __init__(self, status: int, code: str, reason: str | None = None) -> None:
        """Keep status, code and an optional enum reason."""
        super().__init__(code)
        self.status: int = status
        self.code: str = code
        self.reason: str | None = reason


def request_id(request: Request) -> str:
    """Correlation id set by the middleware."""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else "unknown"


def error_body(request: Request, code: str, reason: str | None) -> dict[str, object]:
    """Serialized envelope."""
    return ApiErrorV1(
        schema_version="api_error.v1",
        error=ApiErrorDetail(code=code, reason=reason, request_id=request_id(request)),
    ).model_dump(mode="json")


def _api_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)  # noqa: S101 - registered for ApiError only
    return JSONResponse(
        status_code=error.status, content=error_body(request, error.code, error.reason)
    )


def install_api_errors(
    app: FastAPI,
    legacy_validation: Callable[[Request, Exception], Response] | None = None,
) -> None:
    """Request-id middleware plus ``ApiError``/validation/HTTP handlers."""

    @app.middleware("http")
    async def _with_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = (
            incoming if _REQUEST_ID.match(incoming) else uuid4().hex
        )
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id(request)
        return response

    def _validation(request: Request, error: Exception) -> Response:
        if legacy_validation is not None and not request.url.path.startswith(
            API_PREFIXES
        ):
            return legacy_validation(request, error)
        return JSONResponse(
            status_code=400, content=error_body(request, "invalid_request", None)
        )

    async def _http(request: Request, error: Exception) -> Response:
        assert isinstance(error, StarletteHTTPException)  # noqa: S101
        if not request.url.path.startswith(API_PREFIXES):
            return await http_exception_handler(request, error)
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error"
        )
        return JSONResponse(
            status_code=error.status_code, content=error_body(request, code, None)
        )

    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(StarletteHTTPException, _http)
