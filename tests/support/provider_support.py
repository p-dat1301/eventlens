"""Shared helpers for T6 provider-boundary tests (no network, mock only)."""

# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest

from backend.rag.application.ports.synthesis import SynthesisRequest
from backend.rag.application.synthesis_policies import bundled_activation_policy
from backend.rag.domain.activation import (
    ActivationDecision,
    ActivationPolicy,
    ApiKey,
    CanonicalEndpoint,
)
from backend.rag.domain.model_input import (
    SYNTHESIS_INSTRUCTIONS,
    SYNTHESIS_PROMPT_VERSION,
    ModelInput,
    SerializedModelInput,
    SourceArticlePayload,
    serialize_model_input,
    source_payload,
)
from backend.rag.domain.retrieval import (
    RetrievalMode,
    RetrievedArticle,
    RetrievedSpan,
)
from backend.rag.domain.tokenizer import TokenizerIdentity
from backend.rag.infra.llm.openai_synthesis import TransportResponse
from backend.shared.contracts.evidence import EvidenceSpan

API_KEY_CANARY = "sk-canary-KEY-7f3a9c"
PASSWORD_CANARY = "pw-canary-PASS-91be"  # noqa: S105 - test canary
STORAGE_CANARY = "s3://canary-bucket/raw/STORAGE-URI-2d4e.html"
BODY_CANARY = "BODY-CANARY-provider-secret-5521"
LIVE_TOKENIZER = TokenizerIdentity(tokenizer_id="test-live-tokenizer", version="v7")
LIVE_HOST = "provider.example.invalid"
LIVE_BASE_URL = f"https://{LIVE_HOST}/v1"
CHAT_URL = f"https://{LIVE_HOST}:443/v1/chat/completions"


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any DNS lookup or socket connect fail the test loudly."""

    def blocked(*args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "network access attempted in a no-network test"
        raise AssertionError(message)

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def live_policy_data(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Bundled artifact turned into a live variant (tests only)."""
    data: dict[str, Any] = bundled_activation_policy().model_dump(mode="json")
    data.update(
        policy_id="provider-activation.test-live.v1",
        issuer="eventlens-test-issuer",
        mode="live",
        label="live",
        cost={"basis": "approved_live", "pricing_source": "synthesis_policy.pricing"},
    )
    data["chat_model"] = {
        **data["chat_model"],
        "model_id": "test-chat-model",
        "model_version": "test-chat-model-2026-10-01",
        "tokenizer_id": LIVE_TOKENIZER.tokenizer_id,
        "tokenizer_version": LIVE_TOKENIZER.version,
        "tokenizer_basis": "exact",
    }
    data["embedding_model"] = {
        **data["embedding_model"],
        "model_id": "test-embedding-model",
        "model_version": "test-embedding-model-2026-10-01",
        "tokenizer_basis": "exact",
    }
    data.update(overrides)
    return data


def live_policy(**overrides: Any) -> ActivationPolicy:  # noqa: ANN401
    return ActivationPolicy.model_validate_json(
        json.dumps(live_policy_data(**overrides))
    )


def policy_digest(policy: ActivationPolicy) -> str:
    return sha256(policy.model_dump_json().encode()).hexdigest()


@dataclass
class RecordingRegistry:
    """Test trust registry approving one exact (issuer, policy, digest)."""

    approved: tuple[str, str, str] | None
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def approves(self, issuer: str, policy_id: str, policy_sha256: str) -> bool:
        self.calls.append((issuer, policy_id, policy_sha256))
        return self.approved == (issuer, policy_id, policy_sha256)


def approving(policy: ActivationPolicy) -> RecordingRegistry:
    return RecordingRegistry((policy.issuer, policy.policy_id, policy_digest(policy)))


def live_environ(**overrides: str) -> dict[str, str]:
    env = {
        "EVENTLENS_OPENAI_BASE_URL": LIVE_BASE_URL,
        "EVENTLENS_OPENAI_API_KEY": API_KEY_CANARY,
        "EVENTLENS_CHAT_MODEL": "test-chat-model",
        "EVENTLENS_EMBEDDING_MODEL": "test-embedding-model",
    }
    env.update(overrides)
    return env


def live_decision(policy: ActivationPolicy | None = None) -> ActivationDecision:
    """An approved live decision built directly (cost approval is out of T6)."""
    chosen = policy or live_policy()
    return ActivationDecision(
        mode="live",
        policy_id=chosen.policy_id,
        issuer=chosen.issuer,
        chat_model=chosen.chat_model,
        embedding_model=chosen.embedding_model,
        endpoint=CanonicalEndpoint("https", LIVE_HOST, 443, "/v1"),
        api_key=ApiKey(API_KEY_CANARY),
    )


Step = TransportResponse | BaseException | Callable[..., TransportResponse]


@dataclass
class RecordingTransport:
    """Injected transport returning/raising scripted items; records calls."""

    script: list[Step]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": json.loads(json.dumps(payload)),
                "timeout": timeout_seconds,
            }
        )
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, TransportResponse):
            return step
        return step(url, headers, payload, timeout_seconds)


def completion(  # noqa: PLR0913
    content: object = '{"ok":true}',
    *,
    finish: object = "stop",
    usage: object = None,
    model: object = "test-chat-model",
    refusal: object = None,
    message_extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    message.update(message_extra or {})
    body: dict[str, object] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def ok(body: object, status: int = 200) -> TransportResponse:
    return TransportResponse(status=status, body=json.dumps(body).encode())


def canary_article(question_injection: str = "") -> RetrievedArticle:
    """A retrieved article whose non-allowlisted fields carry canaries."""
    article_id = uuid4()
    text = f"Port closed after storm. {question_injection}".strip()
    digest = f"sha256:{sha256(text.encode()).hexdigest()}"
    span = EvidenceSpan(
        span_id=uuid4(),
        article_id=article_id,
        content_hash=digest,
        start_offset=0,
        end_offset=len(text),
        excerpt=text,
        span_fingerprint=digest,
        offset_basis="unicode_codepoint",
    )
    retrieved_span = RetrievedSpan(
        span=span,
        chunk_id=uuid4(),
        chunk_index=0,
        branches=(),
        lexical_rank=1,
        vector_rank=None,
        fused_score=0.5,
        rerank_score=None,
        truncated=False,
    )
    return RetrievedArticle(
        rank=1,
        article_id=article_id,
        content_hash=digest,
        title="Harbor report",
        source_name="Example Desk",
        source_domain="example.test",
        source_published_at=datetime(2026, 3, 1, tzinfo=UTC),
        retrieval_basis=RetrievalMode.LEXICAL_ONLY,
        vector_generation=None,
        metadata=None,
        duplicate_article_ids=(),
        spans=(retrieved_span,),
    )


def serialized(
    question: str = "What happened at the port?",
    sources: tuple[SourceArticlePayload, ...] | None = None,
    **extra: Any,  # noqa: ANN401
) -> SerializedModelInput:
    if sources is None:
        article = canary_article()
        sources = (source_payload(article, article.spans),)
    return serialize_model_input(
        ModelInput(
            prompt_version=SYNTHESIS_PROMPT_VERSION,
            instructions=SYNTHESIS_INSTRUCTIONS,
            question=question,
            sources=sources,
            **extra,
        )
    )


def request(
    serialized_input: SerializedModelInput | None = None,
    *,
    max_output_tokens: int = 1500,
    timeout_seconds: float = 15.0,
) -> SynthesisRequest:
    return SynthesisRequest(
        serialized=serialized_input or serialized(),
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
