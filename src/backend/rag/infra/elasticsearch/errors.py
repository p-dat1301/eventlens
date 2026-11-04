"""Map transport/HTTP failures onto the port's typed search errors.

Messages carry the HTTP status or error class only, never request bodies,
document text or vectors.
"""

from backend.rag.application.ports.search_index import (
    SearchIndexError,
    SearchIndexUnavailableError,
)

UNAVAILABLE_STATUS = frozenset({408, 429, 502, 503, 504})


def http_error(status: int, error_type: str | None) -> SearchIndexError:
    """Typed error for a non-2xx response."""
    label = f"elasticsearch http {status}" + (f" {error_type}" if error_type else "")
    if status in UNAVAILABLE_STATUS or status >= 500:  # noqa: PLR2004
        return SearchIndexUnavailableError(label)
    return SearchIndexError(label)


def transport_error(error: BaseException) -> SearchIndexUnavailableError:
    """Connection refused, DNS, TLS or timeout."""
    return SearchIndexUnavailableError(
        f"elasticsearch unreachable: {type(error).__name__}"
    )
