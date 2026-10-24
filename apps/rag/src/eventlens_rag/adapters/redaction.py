"""Provider-boundary redaction and payload allowlist guards (D31).

Two guarantees live here:

* ``guard_chat_payload`` checks the exact outbound chat payload before any
  request: fixed top-level keys, trusted system message first, a single user
  message whose JSON holds only the question and the five allowlisted source
  field groups, the frozen strict response format, and no tools. A violation
  raises ``SynthesisConfigurationError`` and nothing is sent.
* ``safe_error_fields`` is the only shape errors take in logs and metrics:
  ``{"code", "reason"}``, never messages, bodies, prompts or credentials.
"""

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, cast

from eventlens_rag.domain.activation import ALLOWED_SOURCE_FIELDS
from eventlens_rag.domain.model_input import SYNTHESIS_INSTRUCTIONS, response_format
from eventlens_rag.domain.synthesis_errors import (
    SynthesisConfigurationError,
    SynthesisError,
)

CHAT_PAYLOAD_KEYS: Final = frozenset(
    {"model", "messages", "response_format", "max_tokens", "temperature", "stream"}
)
USER_KEYS: Final = frozenset({"question", "sources"})
SPAN_KEYS: Final = frozenset({"span_id", "text"})
# JSON keys per allowlisted field group (domain/model_input.py serialization).
SOURCE_KEYS_BY_GROUP: Final[Mapping[str, frozenset[str]]] = {
    "article_id": frozenset({"article_id"}),
    "title": frozenset({"title"}),
    "normalized_content_spans": frozenset({"spans"}),
    "source_name_domain": frozenset({"source_name", "source_domain"}),
    "source_published_at": frozenset({"source_published_at"}),
}
SOURCE_KEYS: Final = frozenset(
    key for group in ALLOWED_SOURCE_FIELDS for key in SOURCE_KEYS_BY_GROUP[group]
)
PAYLOAD_VIOLATION: Final = "model payload violates the provider allowlist"


def safe_error_fields(error: SynthesisError) -> dict[str, str]:
    """Loggable fields of a typed error: stable code and enum reason only."""
    reason: object = getattr(error, "reason", None)
    return {
        "code": type(error).code,
        "reason": str(reason) if isinstance(reason, StrEnum) else "",
    }


def _violation() -> SynthesisConfigurationError:
    return SynthesisConfigurationError(PAYLOAD_VIOLATION)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _violation()
    items = cast("Mapping[object, object]", value)
    return {str(key): item for key, item in items.items()}


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list | tuple):
        raise _violation()
    return list(cast("list[object] | tuple[object, ...]", value))


def _guard_user_content(content: object) -> None:
    if not isinstance(content, str):
        raise _violation()
    try:
        decoded = cast("object", json.loads(content))
        user = _mapping(decoded)
    except json.JSONDecodeError:
        raise _violation() from None
    if frozenset(user) != USER_KEYS or not isinstance(user["question"], str):
        raise _violation()
    for raw_source in _sequence(user["sources"]):
        source = _mapping(raw_source)
        if frozenset(source) != SOURCE_KEYS:
            raise _violation()
        for raw_span in _sequence(source["spans"]):
            if frozenset(_mapping(raw_span)) != SPAN_KEYS:
                raise _violation()


def guard_chat_payload(payload: Mapping[str, object]) -> None:
    """Raise ``SynthesisConfigurationError`` unless the payload is allowlisted."""
    if frozenset(payload) != CHAT_PAYLOAD_KEYS:
        raise _violation()
    if payload["response_format"] != response_format():
        raise _violation()
    if payload["stream"] is not False or payload["temperature"] != 0:
        raise _violation()
    messages = [_mapping(message) for message in _sequence(payload["messages"])]
    expected_roles = ["system", "user"]
    if [message.get("role") for message in messages] != expected_roles:
        raise _violation()
    if any(set(message) != {"role", "content"} for message in messages):
        raise _violation()
    if messages[0]["content"] != SYNTHESIS_INSTRUCTIONS:
        raise _violation()
    _guard_user_content(messages[1]["content"])
