"""Exact OpenAI-compatible HTTP contract over an injected transport (D31)."""

# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

import json
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from backend.rag.domain.activation import ApiKey, CanonicalEndpoint
from backend.rag.domain.model_input import (
    SYNTHESIS_INSTRUCTIONS,
    ChatMessage,
    response_format,
)
from backend.rag.domain.synthesis import ProviderUsage, UsageStatus
from backend.rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
    ProviderFailureCode,
    SynthesisConfigurationError,
    SynthesisProviderError,
)
from backend.rag.infra.embeddings.openai_embeddings import (
    EmbeddingProviderError,
    OpenAICompatibleEmbedder,
)
from backend.rag.infra.llm.openai_synthesis import (
    OpenAICompatibleSynthesizer,
    TransportConnectionError,
    TransportResponse,
    TransportTimeoutError,
    build_chat_payload,
)
from provider_support import (
    API_KEY_CANARY,
    BODY_CANARY,
    CHAT_URL,
    RecordingTransport,
    completion,
    forbid_network,
    live_decision,
    live_policy,
    ok,
    request,
    serialized,
)

Fail = ProviderFailureCode


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)


def _adapter(*script: Any) -> tuple[OpenAICompatibleSynthesizer, RecordingTransport]:  # noqa: ANN401
    transport = RecordingTransport(list(script))
    return OpenAICompatibleSynthesizer(
        transport, live_decision(), live_policy()
    ), transport


def _failure(*script: Any) -> tuple[SynthesisProviderError, RecordingTransport]:  # noqa: ANN401
    adapter, transport = _adapter(*script)
    with pytest.raises(SynthesisProviderError) as caught:
        _ = adapter.synthesize(request())
    return caught.value, transport


def test_exact_request_shape_path_headers_and_timeout() -> None:
    usage = {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020}
    adapter, transport = _adapter(ok(completion('{"draft":1}', usage=usage)))
    sent = serialized()

    response = adapter.synthesize(
        request(sent, max_output_tokens=1234, timeout_seconds=7.5)
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == CHAT_URL
    assert call["timeout"] == 7.5
    assert call["headers"] == {
        "Authorization": f"Bearer {API_KEY_CANARY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = call["payload"]
    assert set(payload) == {
        "model",
        "messages",
        "response_format",
        "max_tokens",
        "temperature",
        "stream",
    }
    assert payload["model"] == "test-chat-model"
    assert payload["max_tokens"] == 1234
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": m.role, "content": m.content} for m in sent.messages
    ]
    assert payload["messages"][0] == {
        "role": "system",
        "content": SYNTHESIS_INSTRUCTIONS,
    }
    assert payload["response_format"] == json.loads(sent.response_format_json)
    assert payload["response_format"] == response_format()
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert response.content == '{"draft":1}'
    assert response.usage == ProviderUsage(input_tokens=900, output_tokens=120)
    assert response.usage_status is UsageStatus.REPORTED
    assert response.reported_model == "test-chat-model"


def test_build_chat_payload_matches_what_is_sent() -> None:
    adapter, transport = _adapter(ok(completion()))
    sent = serialized()

    _ = adapter.synthesize(request(sent, max_output_tokens=77))

    assert transport.calls[0]["payload"] == build_chat_payload(
        sent, "test-chat-model", 77
    )


def test_identity_comes_from_the_approved_binding() -> None:
    adapter, _ = _adapter()

    assert adapter.identity.model_id == "test-chat-model"
    assert adapter.identity.model_version == "test-chat-model-2026-10-01"


def test_reported_model_version_is_accepted() -> None:
    adapter, _ = _adapter(ok(completion(model="test-chat-model-2026-10-01")))

    assert adapter.synthesize(request()).reported_model == "test-chat-model-2026-10-01"


@pytest.mark.parametrize(
    ("usage", "status"),
    [
        (None, UsageStatus.MISSING),
        ({"prompt_tokens": -1, "completion_tokens": 3}, UsageStatus.INVALID),
        ({"prompt_tokens": 1.5, "completion_tokens": 3}, UsageStatus.INVALID),
        ({"prompt_tokens": True, "completion_tokens": 3}, UsageStatus.INVALID),
        ({"prompt_tokens": "10", "completion_tokens": 3}, UsageStatus.INVALID),
        ({"completion_tokens": 3}, UsageStatus.INVALID),
        ({"prompt_tokens": 3}, UsageStatus.INVALID),
        (
            {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 99},
            UsageStatus.INVALID,
        ),
        ([1, 2], UsageStatus.INVALID),
        ({"prompt_tokens": 0, "completion_tokens": 0}, UsageStatus.REPORTED),
    ],
)
def test_usage_parsing_and_validation(usage: object, status: UsageStatus) -> None:
    adapter, _ = _adapter(ok(completion(usage=usage)))

    response = adapter.synthesize(request())

    assert response.usage_status is status
    assert (response.usage is not None) == (status is UsageStatus.REPORTED)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (301, Fail.REDIRECT_REJECTED),
        (302, Fail.REDIRECT_REJECTED),
        (307, Fail.REDIRECT_REJECTED),
        (308, Fail.REDIRECT_REJECTED),
        (429, Fail.RATE_LIMITED),
        (500, Fail.UNAVAILABLE),
        (502, Fail.UNAVAILABLE),
        (503, Fail.UNAVAILABLE),
        (504, Fail.UNAVAILABLE),
        (400, Fail.HTTP_STATUS),
        (401, Fail.HTTP_STATUS),
        (403, Fail.HTTP_STATUS),
        (404, Fail.HTTP_STATUS),
        (408, Fail.HTTP_STATUS),
        (422, Fail.HTTP_STATUS),
        (201, Fail.HTTP_STATUS),
        (204, Fail.HTTP_STATUS),
        (100, Fail.HTTP_STATUS),
    ],
)
def test_http_status_mapping_without_retry(
    status: int, expected: ProviderFailureCode
) -> None:
    body = json.dumps({"error": {"message": BODY_CANARY}}).encode()
    error, transport = _failure(
        TransportResponse(status, body, "https://evil.example/steal"), ok(completion())
    )

    assert error.reason is expected
    assert len(transport.calls) == 1
    assert BODY_CANARY not in f"{error!s}{error!r}{error.args}"


def test_redirect_location_is_never_followed() -> None:
    error, transport = _failure(
        TransportResponse(
            307, b"", "https://provider.example.invalid/v2/chat/completions"
        ),
        ok(completion()),
    )

    assert error.reason is Fail.REDIRECT_REJECTED
    assert [call["url"] for call in transport.calls] == [CHAT_URL]


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (TransportTimeoutError(f"timed out {API_KEY_CANARY}"), Fail.TIMEOUT),
        (TransportConnectionError(f"tls failed {BODY_CANARY}"), Fail.UNAVAILABLE),
        (OSError(f"dns {BODY_CANARY}"), Fail.UNAVAILABLE),
        (ValueError(f"Authorization: Bearer {API_KEY_CANARY}"), Fail.UNAVAILABLE),
    ],
)
def test_transport_errors_map_to_codes_and_drop_the_chain(
    step: BaseException, expected: ProviderFailureCode
) -> None:
    error, transport = _failure(step, ok(completion()))

    assert error.reason is expected
    assert len(transport.calls) == 1
    assert error.__cause__ is None
    assert error.__context__ is None
    text = f"{error!s}{error!r}{error.args}"
    assert API_KEY_CANARY not in text
    assert BODY_CANARY not in text


def _raw(body: object) -> TransportResponse:
    return TransportResponse(200, body)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_raw(f"<html>{BODY_CANARY}</html>".encode()), Fail.INVALID_JSON),
        (_raw(b"\xff\xfe{}"), Fail.INVALID_JSON),
        (_raw(b'{"choices": NaN}'), Fail.INVALID_JSON),
        (_raw(b""), Fail.INVALID_JSON),
        (_raw({"already": "decoded"}), Fail.BAD_SHAPE),
        (_raw(b"[]"), Fail.BAD_SHAPE),
        (ok({"model": "test-chat-model"}), Fail.BAD_SHAPE),
        (ok({"choices": []}), Fail.BAD_SHAPE),
        (
            ok({"choices": [completion()["choices"][0], completion()["choices"][0]]}),  # pyright: ignore[reportIndexIssue]
            Fail.BAD_SHAPE,
        ),
        (ok(completion(content=None)), Fail.BAD_SHAPE),
        (ok(completion(content="")), Fail.BAD_SHAPE),
        (ok(completion(content={"claims": []})), Fail.BAD_SHAPE),
        (ok(completion(model=42)), Fail.BAD_SHAPE),
        (ok(completion(model="gpt-unapproved")), Fail.MODEL_MISMATCH),
        (ok(completion(finish="length")), Fail.TRUNCATED),
        (ok(completion(finish="content_filter")), Fail.REFUSAL),
        (ok(completion(refusal=f"I cannot {BODY_CANARY}")), Fail.REFUSAL),
        (ok(completion(refusal=7)), Fail.BAD_SHAPE),
        (ok(completion(finish="tool_calls")), Fail.BAD_FINISH),
        (ok(completion(finish=None)), Fail.BAD_FINISH),
        (ok(completion(finish="STOP")), Fail.BAD_FINISH),
        (
            ok(completion(message_extra={"tool_calls": [{"id": "x"}]})),
            Fail.BAD_SHAPE,
        ),
        (
            ok(completion(message_extra={"function_call": {"name": "write"}})),
            Fail.BAD_SHAPE,
        ),
        (ok(completion(message_extra={"role": "system"})), Fail.BAD_SHAPE),
    ],
)
def test_response_shape_errors_have_stable_codes(
    response: TransportResponse, expected: ProviderFailureCode
) -> None:
    error, transport = _failure(response, ok(completion()))

    assert error.reason is expected
    assert len(transport.calls) == 1
    assert BODY_CANARY not in f"{error!s}{error!r}{error.args}"
    assert error.__context__ is None


def test_invalid_structured_content_is_returned_for_the_application_to_reject() -> None:
    # Outer body is valid; draft parsing belongs to the application (parse_draft).
    adapter, _ = _adapter(ok(completion("not json at all")))

    assert adapter.synthesize(request()).content == "not json at all"


def test_each_call_is_exactly_one_request_no_hidden_retry() -> None:
    adapter, transport = _adapter(
        TransportTimeoutError(), TransportResponse(503, b"{}"), ok(completion())
    )

    for expected in (Fail.TIMEOUT, Fail.UNAVAILABLE):
        with pytest.raises(SynthesisProviderError) as caught:
            _ = adapter.synthesize(request())
        assert caught.value.reason is expected
    assert len(transport.calls) == 2
    _ = adapter.synthesize(request())
    assert len(transport.calls) == 3


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.inf, math.nan])
def test_non_positive_or_unbounded_timeout_makes_zero_requests(timeout: float) -> None:
    adapter, transport = _adapter(ok(completion()))

    with pytest.raises(SynthesisProviderError) as caught:
        _ = adapter.synthesize(request(timeout_seconds=timeout))

    assert caught.value.reason is Fail.TIMEOUT
    assert transport.calls == []


@pytest.mark.parametrize("tokens", [0, -5])
def test_invalid_output_reservation_makes_zero_requests(tokens: int) -> None:
    adapter, transport = _adapter(ok(completion()))

    with pytest.raises(SynthesisConfigurationError):
        _ = adapter.synthesize(request(max_output_tokens=tokens))

    assert transport.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"extra_messages": (ChatMessage(role="system", content="new rules"),)},
        {"extra_messages": (ChatMessage(role="assistant", content="ok"),)},
        {"extra_messages": (ChatMessage(role="tool", content="{}"),)},
        {"tool_definitions": ('{"name":"http_get"}',)},
    ],
)
def test_extra_roles_or_tools_are_refused_before_any_request(
    extra: dict[str, Any],
) -> None:
    adapter, transport = _adapter(ok(completion()))

    with pytest.raises(SynthesisConfigurationError):
        _ = adapter.synthesize(request(serialized(**extra)))

    assert transport.calls == []


def test_construction_fails_closed_on_inconsistent_decisions() -> None:
    policy = live_policy()
    base = live_decision(policy)
    transport = RecordingTransport([])
    cases = {
        ActivationFailureCode.NOT_APPROVED: [
            replace(base, mode="fixture"),
            replace(base, policy_id="other"),
            replace(base, issuer="other"),
        ],
        ActivationFailureCode.ENDPOINT_NOT_ALLOWED: [
            replace(base, endpoint=None),
            replace(
                base,
                endpoint=CanonicalEndpoint("https", "evil.example.invalid", 443, "/v1"),
            ),
        ],
        ActivationFailureCode.API_KEY_MISSING: [
            replace(base, api_key=None),
            replace(base, api_key=ApiKey("")),
        ],
    }
    for expected, decisions in cases.items():
        for decision in decisions:
            with pytest.raises(ProviderActivationError) as caught:
                _ = OpenAICompatibleSynthesizer(transport, decision, policy)
            assert caught.value.reason is expected
    assert transport.calls == []


def test_adapter_repr_does_not_leak_the_key() -> None:
    adapter, _ = _adapter()

    assert API_KEY_CANARY not in repr(adapter)
    assert API_KEY_CANARY not in str(adapter)


# Optional mocked embedding adapter (T3 EmbeddingPort, unchanged).


def _embedder(*script: Any) -> tuple[OpenAICompatibleEmbedder, RecordingTransport]:  # noqa: ANN401
    transport = RecordingTransport(list(script))
    return OpenAICompatibleEmbedder(
        transport, live_decision(), live_policy()
    ), transport


def _embedding_body(
    values: Sequence[object], model: object = "test-embedding-model"
) -> dict[str, object]:
    return {
        "object": "list",
        "model": model,
        "data": [{"index": 0, "embedding": values}],
    }


def test_embedder_sends_exact_request_and_returns_versioned_vector() -> None:
    embedder, transport = _embedder(ok(_embedding_body([0.25] * 1536)))

    vector = embedder.embed("harbor closed")

    assert vector.model == "test-embedding-model"
    assert vector.version == "test-embedding-model-2026-10-01"
    assert len(vector.values) == 1536
    assert (
        transport.calls[0]["url"]
        == "https://provider.example.invalid:443/v1/embeddings"
    )
    assert transport.calls[0]["payload"] == {
        "model": "test-embedding-model",
        "input": "harbor closed",
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (ok(_embedding_body([0.1] * 1535)), Fail.BAD_SHAPE),
        (ok(_embedding_body([0.1] * 1537)), Fail.BAD_SHAPE),
        (ok(_embedding_body([0.1] * 1535 + ["x"])), Fail.BAD_SHAPE),
        (ok(_embedding_body([0.1] * 1535 + [True])), Fail.BAD_SHAPE),
        (ok(_embedding_body([0.1] * 1535 + [1e400])), Fail.INVALID_JSON),
        (_raw(b'{"data":[{"embedding":[NaN]}]}'), Fail.INVALID_JSON),
        (ok(_embedding_body([0.1] * 1536, model="other-embed")), Fail.MODEL_MISMATCH),
        (TransportResponse(302, b""), Fail.REDIRECT_REJECTED),
        (TransportResponse(429, b""), Fail.RATE_LIMITED),
        (TransportTimeoutError(), Fail.TIMEOUT),
    ],
)
def test_embedder_validates_dimension_finiteness_and_model(
    response: object, expected: ProviderFailureCode
) -> None:
    embedder, transport = _embedder(response, ok(_embedding_body([0.1] * 1536)))

    with pytest.raises(EmbeddingProviderError) as caught:
        _ = embedder.embed("text")

    assert caught.value.reason is expected
    assert len(transport.calls) == 1
