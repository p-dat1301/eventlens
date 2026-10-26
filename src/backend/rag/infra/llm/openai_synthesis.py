"""OpenAI-compatible grounded synthesis over an injected transport (D31).

Frozen HTTP contract (``provider_activation_policy.v1`` transport section):

* ``POST {endpoint}{transport.chat_path}`` (``/chat/completions``) on the
  canonical allowlisted endpoint chosen by activation; one request per
  ``synthesize``; no redirects, no retries, no env proxy, TLS verified.
* Headers: ``Authorization: Bearer <key>``, ``Content-Type`` and ``Accept``
  ``application/json``.
* Body: ``model`` (approved chat model ID), ``messages`` exactly
  ``serialized.messages`` (system instructions + one user JSON message),
  ``response_format`` exactly ``serialized.response_format_json`` (strict JSON
  schema from ``draft_json_schema``), ``max_tokens`` = output reservation,
  ``temperature`` 0, ``stream`` false. No tools.
* Timeout: ``request.timeout_seconds`` (the caller passes
  ``min(call_timeout_seconds, control.remaining())``).
* Response: status 200 only; raw body must decode as UTF-8 JSON; exactly one
  choice with an assistant message, no tool calls, ``finish_reason`` ``stop``
  and string content. ``usage.prompt_tokens`` -> input,
  ``usage.completion_tokens`` -> output.

Status mapping: 3xx -> ``provider_redirect_rejected`` (never followed); 429 ->
``provider_rate_limited``; 5xx -> ``provider_unavailable``; any other non-200
-> ``provider_http_status``. Errors keep only the code: the exception chain
is cut so no body, header, key or prompt survives in tracebacks or logs.
No HTTP client ships with this module, so nothing can reach a provider
unless composition injects a transport and an approved live decision.
"""

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.application.use_cases.activation import endpoint_allowed
from backend.rag.domain.activation import (
    ActivationDecision,
    ActivationPolicy,
    CanonicalEndpoint,
)
from backend.rag.domain.model_input import SerializedModelInput
from backend.rag.domain.synthesis import ChatModelIdentity, ProviderUsage, UsageStatus
from backend.rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
    ProviderFailureCode,
    SynthesisConfigurationError,
    SynthesisProviderError,
)
from backend.rag.infra.llm.redaction import guard_chat_payload

HTTP_OK: Final = 200
HTTP_REDIRECT_MIN: Final = 300
HTTP_REDIRECT_MAX: Final = 399
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR_MIN: Final = 500
HTTP_SERVER_ERROR_MAX: Final = 599

LOGGER: Final = logging.getLogger(__name__)
REQUEST_VIOLATION: Final = "synthesis request violates the provider contract"


class TransportTimeoutError(Exception):
    """The transport deadline elapsed before a response arrived."""


class TransportConnectionError(Exception):
    """Connection, DNS or TLS failure; the transport must never downgrade TLS."""


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """One raw HTTP response; ``body`` is the undecoded bytes/str payload."""

    status: int
    body: object
    location: str | None = None


class SynthesisTransport(Protocol):
    """Single HTTPS POST with a hard timeout.

    Implementations must not retry, must not follow redirects (return the
    3xx response instead), must verify TLS and must ignore ambient proxy
    environment variables (``trust_env=False``).
    """

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        """POST ``payload`` as JSON and return the raw response."""
        ...


def build_chat_payload(
    serialized: SerializedModelInput, model: str, max_output_tokens: int
) -> dict[str, object]:
    """Exact outbound chat body for one serialized model input."""
    return {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in serialized.messages
        ],
        "response_format": json.loads(serialized.response_format_json),
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "stream": False,
    }


def _status_code(status: int) -> ProviderFailureCode | None:
    if status == HTTP_OK:
        return None
    if HTTP_REDIRECT_MIN <= status <= HTTP_REDIRECT_MAX:
        return ProviderFailureCode.REDIRECT_REJECTED
    if status == HTTP_TOO_MANY_REQUESTS:
        return ProviderFailureCode.RATE_LIMITED
    if HTTP_SERVER_ERROR_MIN <= status <= HTTP_SERVER_ERROR_MAX:
        return ProviderFailureCode.UNAVAILABLE
    return ProviderFailureCode.HTTP_STATUS


class _Failure(Exception):  # noqa: N818 - internal control-flow signal
    def __init__(self, code: ProviderFailureCode) -> None:
        super().__init__(code.value)
        self.code: ProviderFailureCode = code


def _reject_constant(_: str) -> object:
    raise _Failure(ProviderFailureCode.INVALID_JSON)


def _decode(body: object) -> object:
    if isinstance(body, bytes | bytearray):
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            raise _Failure(ProviderFailureCode.INVALID_JSON) from None
    elif isinstance(body, str):
        text = body
    else:
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    try:
        decoded = cast("object", json.loads(text, parse_constant=_reject_constant))
    except (json.JSONDecodeError, RecursionError):
        raise _Failure(ProviderFailureCode.INVALID_JSON) from None
    return decoded


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    items = cast("dict[object, object]", value)
    return {str(key): item for key, item in items.items()}


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage(body: Mapping[str, object]) -> tuple[ProviderUsage | None, UsageStatus]:
    raw = body.get("usage")
    if raw is None:
        return None, UsageStatus.MISSING
    if not isinstance(raw, dict):
        return None, UsageStatus.INVALID
    usage = _object(cast("object", raw))
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not (_is_count(prompt) and _is_count(completion)):
        return None, UsageStatus.INVALID
    assert isinstance(prompt, int)  # noqa: S101 - narrowed by _is_count
    assert isinstance(completion, int)  # noqa: S101 - narrowed by _is_count
    total = usage.get("total_tokens")
    if total is not None and (not _is_count(total) or total != prompt + completion):
        return None, UsageStatus.INVALID
    return ProviderUsage(input_tokens=prompt, output_tokens=completion), (
        UsageStatus.REPORTED
    )


def _no_tool_calls(message: Mapping[str, object]) -> None:
    for key in ("tool_calls", "function_call"):
        if message.get(key) not in (None, []):
            raise _Failure(ProviderFailureCode.BAD_SHAPE)


def _content(body: Mapping[str, object]) -> str:
    raw_choices = body.get("choices")
    if not isinstance(raw_choices, list):
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    choices = cast("list[object]", raw_choices)
    if len(choices) != 1:
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    choice = _object(choices[0])
    message = _object(choice.get("message"))
    if message.get("role") != "assistant":
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    refusal = message.get("refusal")
    finish = choice.get("finish_reason")
    if (isinstance(refusal, str) and refusal) or finish == "content_filter":
        raise _Failure(ProviderFailureCode.REFUSAL)
    if refusal is not None and not isinstance(refusal, str):
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    _no_tool_calls(message)
    if finish == "length":
        raise _Failure(ProviderFailureCode.TRUNCATED)
    if finish != "stop":
        raise _Failure(ProviderFailureCode.BAD_FINISH)
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    return content


@dataclass(frozen=True, slots=True)
class _Parsed:
    content: str
    usage: ProviderUsage | None
    usage_status: UsageStatus
    reported_model: str | None


def _parse(response: TransportResponse, accepted_models: frozenset[str]) -> _Parsed:
    code = _status_code(response.status)
    if code is not None:
        raise _Failure(code)
    body = _object(_decode(response.body))
    reported = body.get("model")
    if reported is not None and not isinstance(reported, str):
        raise _Failure(ProviderFailureCode.BAD_SHAPE)
    if reported is not None and reported not in accepted_models:
        raise _Failure(ProviderFailureCode.MODEL_MISMATCH)
    content = _content(body)
    usage, status = _usage(body)
    return _Parsed(content, usage, status, reported)


def _activation_error(reason: ActivationFailureCode) -> ProviderActivationError:
    return ProviderActivationError(reason)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSynthesizer:
    """``SynthesisModelPort`` adapter: one POST, typed failures, no retry.

    Construction requires an approved live ``ActivationDecision`` whose
    endpoint is allowlisted by the same policy; otherwise it raises
    ``ProviderActivationError`` and no transport call can ever happen.
    """

    transport: SynthesisTransport
    decision: ActivationDecision
    policy: ActivationPolicy
    _endpoint: CanonicalEndpoint = field(init=False, repr=False)
    _headers: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Fail closed unless the decision is a consistent live approval."""
        decision = self.decision
        if decision.mode != "live" or decision.policy_id != self.policy.policy_id:
            raise _activation_error(ActivationFailureCode.NOT_APPROVED)
        if (
            decision.issuer != self.policy.issuer
            or decision.chat_model != self.policy.chat_model
        ):
            raise _activation_error(ActivationFailureCode.NOT_APPROVED)
        endpoint = decision.endpoint
        if endpoint is None or not endpoint_allowed(endpoint, self.policy.endpoints):
            raise _activation_error(ActivationFailureCode.ENDPOINT_NOT_ALLOWED)
        key = decision.api_key
        if key is None or not key.value:
            raise _activation_error(ActivationFailureCode.API_KEY_MISSING)
        object.__setattr__(self, "_endpoint", endpoint)
        object.__setattr__(
            self,
            "_headers",
            {
                "Authorization": f"Bearer {key.value}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    @property
    def identity(self) -> ChatModelIdentity:
        """Approved chat model identity from the activation decision."""
        binding = self.decision.chat_model
        return ChatModelIdentity(
            model_id=binding.model_id, model_version=binding.model_version
        )

    def _payload(self, request: SynthesisRequest) -> dict[str, object]:
        tokens = request.max_output_tokens
        timeout = request.timeout_seconds
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise SynthesisConfigurationError(REQUEST_VIOLATION)
        if request.serialized.tool_definitions:
            raise SynthesisConfigurationError(REQUEST_VIOLATION)
        if not math.isfinite(timeout) or timeout <= 0:
            raise SynthesisProviderError(ProviderFailureCode.TIMEOUT)
        payload = build_chat_payload(
            request.serialized, self.decision.chat_model.model_id, tokens
        )
        guard_chat_payload(payload)
        return payload

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """POST once; map every failure to a stable ``ProviderFailureCode``."""
        payload = self._payload(request)
        url = self._endpoint.url(self.policy.transport.chat_path)
        failure: ProviderFailureCode | None = None
        response: TransportResponse | None = None
        try:
            response = self.transport.post_json(
                url, self._headers, payload, request.timeout_seconds
            )
        except TransportTimeoutError:
            failure = ProviderFailureCode.TIMEOUT
        except Exception:  # noqa: BLE001 - transport boundary: drop everything but a code
            failure = ProviderFailureCode.UNAVAILABLE
        parsed: _Parsed | None = None
        if failure is None:
            failure, parsed = self._interpret(response)
        if failure is not None or parsed is None:
            code = failure or ProviderFailureCode.BAD_SHAPE
            LOGGER.warning("synthesis provider failed", extra={"provider_code": code})
            # Raised outside every except block: no __cause__/__context__ chain.
            raise SynthesisProviderError(code)
        return SynthesisResponse(
            content=parsed.content,
            usage=parsed.usage,
            usage_status=parsed.usage_status,
            reported_model=parsed.reported_model,
        )

    def _interpret(
        self, response: TransportResponse | None
    ) -> tuple[ProviderFailureCode | None, _Parsed | None]:
        if not isinstance(response, TransportResponse):
            return ProviderFailureCode.BAD_SHAPE, None
        binding = self.decision.chat_model
        accepted = frozenset({binding.model_id, binding.model_version})
        try:
            return None, _parse(response, accepted)
        except _Failure as error:
            return error.code, None
