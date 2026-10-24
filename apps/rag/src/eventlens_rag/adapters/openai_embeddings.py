"""OpenAI-compatible embeddings over an injected transport (mock-only in T6).

Implements the T3 ``EmbeddingPort`` unchanged; no new port or schema. Frozen
contract: ``POST {endpoint}{transport.embeddings_path}`` with body
``{"model", "input"}``; status 200 only; exactly one ``data`` item whose
``embedding`` has the approved dimension (1536) of finite numbers; the
reported model, when present, must equal the approved model ID or version.
One request per ``embed``, no retries, no redirects; failures raise
``EmbeddingProviderError`` with a stable ``ProviderFailureCode`` only.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, cast

from eventlens_rag.adapters.openai_synthesis import (
    HTTP_OK,
    HTTP_REDIRECT_MAX,
    HTTP_REDIRECT_MIN,
    HTTP_SERVER_ERROR_MAX,
    HTTP_SERVER_ERROR_MIN,
    HTTP_TOO_MANY_REQUESTS,
    SynthesisTransport,
    TransportResponse,
    TransportTimeoutError,
)
from eventlens_rag.application.activation import endpoint_allowed
from eventlens_rag.domain.activation import (
    ActivationDecision,
    ActivationPolicy,
    CanonicalEndpoint,
)
from eventlens_rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
    ProviderFailureCode,
)
from eventlens_rag.ports.embeddings import EmbeddingVector

DEFAULT_EMBEDDING_TIMEOUT_SECONDS: Final = 15.0


class EmbeddingProviderError(RuntimeError):
    """Embedding provider failure carrying only a stable code."""

    def __init__(self, reason: ProviderFailureCode) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: ProviderFailureCode = reason


def _status(status: int) -> ProviderFailureCode | None:
    if status == HTTP_OK:
        return None
    if HTTP_REDIRECT_MIN <= status <= HTTP_REDIRECT_MAX:
        return ProviderFailureCode.REDIRECT_REJECTED
    if status == HTTP_TOO_MANY_REQUESTS:
        return ProviderFailureCode.RATE_LIMITED
    if HTTP_SERVER_ERROR_MIN <= status <= HTTP_SERVER_ERROR_MAX:
        return ProviderFailureCode.UNAVAILABLE
    return ProviderFailureCode.HTTP_STATUS


def _reject_constant(_: str) -> object:
    raise ValueError


def _decode(body: object) -> object:
    if isinstance(body, bytes | bytearray):
        try:
            body = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return ProviderFailureCode.INVALID_JSON
    if not isinstance(body, str):
        return ProviderFailureCode.BAD_SHAPE
    try:
        decoded = cast("object", json.loads(body, parse_constant=_reject_constant))
    except (ValueError, RecursionError):
        return ProviderFailureCode.INVALID_JSON
    return decoded


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    items = cast("dict[object, object]", value)
    return {str(key): item for key, item in items.items()}


def _as_list(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def _vector(
    decoded: object, accepted: frozenset[str], dimension: int
) -> tuple[float, ...] | ProviderFailureCode:
    body = _as_dict(decoded)
    if body is None:
        return ProviderFailureCode.BAD_SHAPE
    reported = body.get("model")
    if reported is not None and reported not in accepted:
        return ProviderFailureCode.MODEL_MISMATCH
    data = _as_list(body.get("data"))
    item = _as_dict(data[0]) if data is not None and len(data) == 1 else None
    raw = None if item is None else _as_list(item.get("embedding"))
    if raw is None or len(raw) != dimension:
        return ProviderFailureCode.BAD_SHAPE
    values: list[float] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int | float):
            return ProviderFailureCode.BAD_SHAPE
        number = float(entry)
        if not math.isfinite(number):
            return ProviderFailureCode.BAD_SHAPE
        values.append(number)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEmbedder:
    """``EmbeddingPort`` adapter bound to an approved live activation."""

    transport: SynthesisTransport
    decision: ActivationDecision
    policy: ActivationPolicy
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS
    _endpoint: CanonicalEndpoint = field(init=False, repr=False)
    _headers: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Fail closed unless the decision is a consistent live approval."""
        decision = self.decision
        if (
            decision.mode != "live"
            or decision.policy_id != self.policy.policy_id
            or decision.embedding_model != self.policy.embedding_model
        ):
            raise ProviderActivationError(ActivationFailureCode.NOT_APPROVED)
        endpoint = decision.endpoint
        if endpoint is None or not endpoint_allowed(endpoint, self.policy.endpoints):
            raise ProviderActivationError(ActivationFailureCode.ENDPOINT_NOT_ALLOWED)
        if decision.api_key is None or not decision.api_key.value:
            raise ProviderActivationError(ActivationFailureCode.API_KEY_MISSING)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ProviderActivationError(ActivationFailureCode.POLICY_INVALID)
        object.__setattr__(self, "_endpoint", endpoint)
        object.__setattr__(
            self,
            "_headers",
            {
                "Authorization": f"Bearer {decision.api_key.value}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def embed(self, text: str) -> EmbeddingVector:
        """POST once and validate model, dimension and finite values."""
        binding = self.decision.embedding_model
        payload = {"model": binding.model_id, "input": text}
        url = self._endpoint.url(self.policy.transport.embeddings_path)
        result: tuple[float, ...] | ProviderFailureCode
        try:
            response = self.transport.post_json(
                url, self._headers, payload, self.timeout_seconds
            )
        except TransportTimeoutError:
            result = ProviderFailureCode.TIMEOUT
        except Exception:  # noqa: BLE001 - transport boundary: keep only a code
            result = ProviderFailureCode.UNAVAILABLE
        else:
            result = self._interpret(response)
        if isinstance(result, ProviderFailureCode):
            raise EmbeddingProviderError(result)
        return EmbeddingVector(
            model=binding.model_id, version=binding.model_version, values=result
        )

    def _interpret(self, response: object) -> tuple[float, ...] | ProviderFailureCode:
        if not isinstance(response, TransportResponse):
            return ProviderFailureCode.BAD_SHAPE
        code = _status(response.status)
        if code is not None:
            return code
        decoded = _decode(response.body)
        if isinstance(decoded, ProviderFailureCode):
            return decoded
        binding = self.decision.embedding_model
        dimension = binding.embedding_dimension
        if dimension is None:
            return ProviderFailureCode.BAD_SHAPE
        accepted = frozenset({binding.model_id, binding.model_version})
        return _vector(decoded, accepted, dimension)
