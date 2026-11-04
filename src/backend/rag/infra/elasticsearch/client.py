"""Minimal JSON/NDJSON HTTP client for Elasticsearch (stdlib only).

Production endpoints must be HTTPS with an API key; plain HTTP is accepted
only for loopback hosts (local compose, tests, offline benchmarks).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from backend.rag.infra.elasticsearch.errors import http_error, transport_error

if TYPE_CHECKING:
    from http.client import HTTPResponse

type Json = dict[str, object]
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class InsecureEndpointError(ValueError):
    """Non-loopback endpoints must use HTTPS and an API key."""

    def __init__(self) -> None:
        """Constant message: the URL may contain credentials."""
        super().__init__("elasticsearch endpoint must be https with an api key")


def validate_endpoint(base_url: str, api_key: str | None) -> None:
    """Allow ``http://`` only on loopback; otherwise require https + key."""
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise InsecureEndpointError
    if parts.username or parts.password:
        raise InsecureEndpointError
    if parts.hostname in LOOPBACK:
        return
    if parts.scheme != "https" or not api_key:
        raise InsecureEndpointError


@dataclass(frozen=True, slots=True)
class ElasticsearchClient:
    """One endpoint; every call is bounded by ``timeout_seconds``."""

    base_url: str
    api_key: str | None = None
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Refuse insecure non-loopback endpoints before any request."""
        validate_endpoint(self.base_url, self.api_key)

    def _headers(self, content_type: str) -> dict[str, str]:
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Json]:
        """JSON request; statuses in ``allow`` are returned instead of raised."""
        data = None if body is None else json.dumps(body).encode()
        return self._send(method, path, data, "application/json", allow)

    def bulk(self, path: str, lines: Sequence[Mapping[str, object]]) -> Json:
        """NDJSON ``_bulk`` request."""
        payload = "".join(json.dumps(line) + "\n" for line in lines).encode()
        _, response = self._send("POST", path, payload, "application/x-ndjson", ())
        return response

    def _send(
        self,
        method: str,
        path: str,
        data: bytes | None,
        content_type: str,
        allow: Sequence[int],
    ) -> tuple[int, Json]:
        request = Request(  # noqa: S310 - scheme validated in __post_init__
            self.base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers=self._headers(content_type),
        )
        try:
            response = cast(
                "HTTPResponse",
                urlopen(request, timeout=self.timeout_seconds),  # noqa: S310
            )
            with response:
                raw = response.read()
                status = response.status
        except HTTPError as error:
            status = error.code
            raw = error.read()
            if status not in allow:
                raise http_error(status, _error_type(raw)) from None
        except (URLError, TimeoutError, OSError) as error:
            raise transport_error(error) from None
        if not raw:
            return status, {}
        parsed = cast("object", json.loads(raw))
        return status, cast("Json", parsed) if isinstance(parsed, dict) else {}


def _error_type(raw: bytes) -> str | None:
    try:
        parsed = cast("object", json.loads(raw))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = cast("dict[str, object]", parsed).get("error")
    if isinstance(error, dict):
        kind = cast("dict[str, object]", error).get("type")
        return kind if isinstance(kind, str) else None
    return None
