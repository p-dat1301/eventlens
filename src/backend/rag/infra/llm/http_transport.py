"""Stdlib JSON POST transport for OpenAI-compatible endpoints.

Built only by the composition root when a provider is explicitly configured.
No retries, no redirects (3xx is returned as a response), no ambient proxy
variables, TLS verified by the default SSL context, hard timeout. Response
bodies are returned raw and never logged.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, cast, override

from backend.rag.infra.llm.openai_synthesis import (
    TransportConnectionError,
    TransportResponse,
    TransportTimeoutError,
)

if TYPE_CHECKING:
    from http.client import HTTPResponse

MAX_RESPONSE_BYTES: Final = 2_000_000


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


class UrllibJsonTransport:
    """``SynthesisTransport`` implementation without ambient proxies."""

    def __init__(self) -> None:
        """Opener with an empty proxy map and no redirect following."""
        self._opener: urllib.request.OpenerDirector = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        """POST ``payload`` and return status, raw body and Location."""
        if not url.startswith(("http://", "https://")):
            raise TransportConnectionError
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=dict(headers),
            method="POST",
        )
        try:
            response = cast(
                "HTTPResponse", self._opener.open(request, timeout=timeout_seconds)
            )
            with response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
                location = response.getheader("Location")
        except urllib.error.HTTPError as error:
            return TransportResponse(error.code, b"", None)
        except TimeoutError:
            raise TransportTimeoutError from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportTimeoutError from None
            raise TransportConnectionError from None
        except OSError:
            raise TransportConnectionError from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise TransportConnectionError
        return TransportResponse(status, body, location)
