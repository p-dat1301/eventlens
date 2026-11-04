"""HTTP adapter of ``ArticleCatalogPort`` over the RAG internal catalog API.

``GET {base}/internal/v1/catalog/...`` with ``Authorization: Bearer <service
token>``, a short deadline and a small circuit breaker. Responses are
validated strictly against ``catalog.v1``. Typed 4xx rejections (bad filter,
404, 409 stale version, 410 removed) pass through; everything else becomes
``CatalogUnavailableError`` (never an empty page). No retries; no URL,
token or response body is ever placed in an exception or log line.
"""

# ruff: noqa: EM101

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast, override
from uuid import UUID

from pydantic import BaseModel, ValidationError

from backend.crud.application.ports.article_catalog import (
    CatalogContractError,
    CatalogUnavailableError,
    CatalogUpstreamError,
)
from backend.crud.domain.catalog import ArticleListParams, CatalogFilterParams
from backend.shared.contracts.catalog import (
    ApiErrorV1,
    ArticleDetailV1,
    ArticleFacetsV1,
    ArticleListV1,
    ArticleSummaryV1,
    LocationListV1,
    SourceListV1,
    TopicListV1,
)

if TYPE_CHECKING:
    from http.client import HTTPResponse

PREFIX: Final = "/internal/v1/catalog"
PASS_THROUGH: Final = frozenset({400, 404, 409, 410})
MAX_BODY: Final = 8_000_000
HTTP_OK: Final = 200


class CircuitBreaker:
    """Open after ``threshold`` consecutive failures for ``cooldown`` seconds."""

    def __init__(
        self,
        threshold: int = 5,
        cooldown_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Closed initially."""
        self._threshold: int = threshold
        self._cooldown: float = cooldown_seconds
        self._clock: Callable[[], float] = clock
        self._failures: int = 0
        self._opened_at: float | None = None
        self._lock: threading.Lock = threading.Lock()

    def allow(self) -> bool:
        """Whether a call may proceed (half-open after the cooldown)."""
        with self._lock:
            if self._opened_at is None:
                return True
            return self._clock() - self._opened_at >= self._cooldown

    def record(self, *, ok: bool) -> None:
        """Track the outcome of one call."""
        with self._lock:
            if ok:
                self._failures, self._opened_at = 0, None
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


class RagCatalogHttp:
    """``ArticleCatalogPort`` implementation."""

    def __init__(
        self,
        base_url: str,
        service_token: str | None,
        timeout_seconds: float = 3.0,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        """Bind the internal base URL and the service credential."""
        self._base: str = base_url.rstrip("/")
        self._token: str | None = service_token
        self._timeout: float = timeout_seconds
        self._breaker: CircuitBreaker = breaker or CircuitBreaker()
        self._opener: urllib.request.OpenerDirector = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _get[M: BaseModel](
        self, path: str, params: tuple[tuple[str, str], ...], model: type[M]
    ) -> M:
        if not self._token:
            raise CatalogUnavailableError("service_token_not_configured")
        if not self._breaker.allow():
            raise CatalogUnavailableError("circuit_open")
        query = urllib.parse.urlencode(params)
        url = f"{self._base}{PREFIX}{path}" + (f"?{query}" if query else "")
        if not url.startswith(("http://", "https://")):
            raise CatalogUnavailableError("invalid_catalog_url")
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        status, body = self._fetch(request)
        if status == HTTP_OK:
            try:
                parsed = model.model_validate_json(body)
            except ValidationError:
                self._breaker.record(ok=True)
                raise CatalogContractError from None
            self._breaker.record(ok=True)
            return parsed
        if status in PASS_THROUGH:
            self._breaker.record(ok=True)
            try:
                error = ApiErrorV1.model_validate_json(body).error
            except ValidationError:
                raise CatalogContractError from None
            raise CatalogUpstreamError(status, error.code, error.reason)
        self._breaker.record(ok=False)
        reason = {401: "service_auth_failed", 403: "service_auth_failed"}.get(
            status, "upstream_error"
        )
        raise CatalogUnavailableError(reason)

    def _fetch(self, request: urllib.request.Request) -> tuple[int, bytes]:
        try:
            response = cast(
                "HTTPResponse", self._opener.open(request, timeout=self._timeout)
            )
            with response:
                return response.status, response.read(MAX_BODY)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(MAX_BODY)
        except (TimeoutError, urllib.error.URLError, OSError):
            self._breaker.record(ok=False)
            raise CatalogUnavailableError("catalog_unreachable") from None

    def list_articles(self, params: ArticleListParams) -> ArticleListV1:
        """``GET /articles``."""
        return self._get("/articles", params.items(), ArticleListV1)

    def get_article(
        self, article_id: UUID, article_version_id: str | None
    ) -> ArticleDetailV1:
        """``GET /articles/{id}``."""
        params = (
            ()
            if article_version_id is None
            else (("article_version_id", article_version_id),)
        )
        return self._get(f"/articles/{article_id}", params, ArticleDetailV1)

    def get_summary(self, article_id: UUID) -> ArticleSummaryV1:
        """``GET /articles/{id}/summary``."""
        return self._get(f"/articles/{article_id}/summary", (), ArticleSummaryV1)

    def facets(self, filters: CatalogFilterParams) -> ArticleFacetsV1:
        """``GET /facets``."""
        return self._get("/facets", filters.items(), ArticleFacetsV1)

    def topics(self) -> TopicListV1:
        """``GET /topics``."""
        return self._get("/topics", (), TopicListV1)

    def locations(
        self, parent_id: str | None, q: str | None, limit: int
    ) -> LocationListV1:
        """``GET /locations``."""
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if parent_id is not None:
            params.append(("parent_id", parent_id))
        if q is not None:
            params.append(("q", q))
        return self._get("/locations", tuple(params), LocationListV1)

    def sources(self) -> SourceListV1:
        """``GET /sources``."""
        return self._get("/sources", (), SourceListV1)
