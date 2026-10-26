"""D31 activation: fixture default, fail-closed live, zero outbound requests."""

# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

import json
from collections.abc import Callable
from typing import Any

import pytest

from backend.rag.application.ports.synthesis import TrustedIssuerRegistry
from backend.rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_activation_policy_sha256,
    bundled_synthesis_policy,
)
from backend.rag.application.use_cases.activation import (
    activate,
    evaluate_activation,
    read_provider_environment,
)
from backend.rag.domain.activation import ActivationPolicy
from backend.rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
)
from backend.rag.domain.synthesis_policy import SynthesisPolicy
from backend.rag.infra.access.trust import NoTrustedIssuers
from backend.rag.infra.llm.fixture_tokens import FIXTURE_TOKENIZER
from backend.rag.infra.llm.openai_synthesis import OpenAICompatibleSynthesizer
from provider_support import (
    API_KEY_CANARY,
    LIVE_TOKENIZER,
    RecordingRegistry,
    RecordingTransport,
    approving,
    completion,
    forbid_network,
    live_environ,
    live_policy,
    live_policy_data,
    ok,
    policy_digest,
)

Code = ActivationFailureCode


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)


def _other_synthesis_policy() -> SynthesisPolicy:
    data = bundled_synthesis_policy().model_dump(mode="json")
    data["policy_id"] = "synthesis-policy.other"
    return SynthesisPolicy.model_validate_json(json.dumps(data))


def _live(  # noqa: PLR0913
    policy: ActivationPolicy | None = None,
    environ: dict[str, str] | None = None,
    *,
    trust: TrustedIssuerRegistry | None = None,
    synthesis: SynthesisPolicy | None = None,
    digest: str | None = None,
    counter: Any = LIVE_TOKENIZER,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    chosen = policy or live_policy()
    return activate(
        chosen,
        digest if digest is not None else policy_digest(chosen),
        synthesis or bundled_synthesis_policy(),
        live_environ() if environ is None else environ,
        "live",
        trust if trust is not None else approving(chosen),
        counter,
    )


def _reason(call: Callable[[], object]) -> ActivationFailureCode:
    with pytest.raises(ProviderActivationError) as caught:
        _ = call()
    return caught.value.reason


def test_fixture_is_default_and_env_key_never_enables_live() -> None:
    registry = RecordingRegistry(approved=None)
    environ = live_environ(HTTPS_PROXY="http://proxy.invalid:3128")

    decision = activate(
        bundled_activation_policy(),
        bundled_activation_policy_sha256(),
        bundled_synthesis_policy(),
        environ,
        "fixture",
        registry,
        FIXTURE_TOKENIZER,
    )

    assert decision.mode == "fixture"
    assert decision.endpoint is None
    assert decision.api_key is None
    assert decision.policy_id == "provider-activation.fixture.v1"
    assert registry.calls == []


def test_env_reads_only_the_four_plan_variables() -> None:
    environment = read_provider_environment(
        {**live_environ(), "OPENAI_API_KEY": "other", "EVENTLENS_OPENAI_ORG": "x"}
    )

    assert environment.base_url == "https://provider.example.invalid/v1"
    assert environment.api_key is not None
    assert environment.api_key.value == API_KEY_CANARY
    assert API_KEY_CANARY not in repr(environment)
    assert API_KEY_CANARY not in str(environment.api_key)
    assert environment.chat_model == "test-chat-model"
    blank = read_provider_environment({"EVENTLENS_OPENAI_API_KEY": "  "})
    assert blank.api_key is None


def test_bundled_fixture_policy_never_approves_live_even_with_key() -> None:
    reason = _reason(
        lambda: activate(
            bundled_activation_policy(),
            bundled_activation_policy_sha256(),
            bundled_synthesis_policy(),
            live_environ(),
            "live",
            NoTrustedIssuers(),
            FIXTURE_TOKENIZER,
        )
    )
    assert reason is Code.NOT_APPROVED


def test_live_policy_with_default_registry_is_untrusted() -> None:
    assert _reason(lambda: _live(trust=NoTrustedIssuers())) is Code.UNTRUSTED_ISSUER


def test_registry_must_approve_the_exact_digest() -> None:
    policy = live_policy()
    registry = RecordingRegistry((policy.issuer, policy.policy_id, "0" * 64))

    assert _reason(lambda: _live(policy, trust=registry)) is Code.UNTRUSTED_ISSUER
    assert registry.calls == [(policy.issuer, policy.policy_id, policy_digest(policy))]


def test_live_policy_in_fixture_mode_is_invalid() -> None:
    policy = live_policy()
    reason = _reason(
        lambda: evaluate_activation(
            policy,
            policy_digest(policy),
            bundled_synthesis_policy(),
            read_provider_environment({}),
            "fixture",
            NoTrustedIssuers(),
            FIXTURE_TOKENIZER,
        )
    )
    assert reason is Code.POLICY_INVALID


def test_fixture_mode_still_rejects_budget_binding_mismatch() -> None:
    reason = _reason(
        lambda: evaluate_activation(
            bundled_activation_policy(),
            bundled_activation_policy_sha256(),
            _other_synthesis_policy(),
            read_provider_environment({}),
            "fixture",
            NoTrustedIssuers(),
            FIXTURE_TOKENIZER,
        )
    )
    assert reason is Code.BUDGET_POLICY_MISMATCH


def test_fully_consistent_live_config_still_needs_approved_cost_policy() -> None:
    # Every earlier check passes; fixture pricing can never price a live run.
    assert _reason(_live) is Code.COST_POLICY_MISSING


def _tls_disabled() -> ActivationPolicy:
    policy = live_policy()
    transport = policy.transport.model_copy(update={"tls_verify": False})
    return policy.model_copy(update={"transport": transport})


def _proxy_enabled() -> ActivationPolicy:
    policy = live_policy()
    transport = policy.transport.model_copy(update={"trust_env_proxy": True})
    return policy.model_copy(update={"transport": transport})


def _redirects_enabled() -> ActivationPolicy:
    policy = live_policy()
    transport = policy.transport.model_copy(update={"follow_redirects": True})
    return policy.model_copy(update={"transport": transport})


def _prohibited_without_hash() -> ActivationPolicy:
    data = live_policy_data()
    prohibited = [
        item for item in data["corpus"]["prohibited"] if item != "content_hash"
    ]
    data["corpus"] = {**data["corpus"], "prohibited": prohibited}
    return ActivationPolicy.model_validate_json(json.dumps(data))


def _no_corpus() -> ActivationPolicy:
    data = live_policy_data()
    data["corpus"] = {**data["corpus"], "corpus_ids": []}
    return ActivationPolicy.model_validate_json(json.dumps(data))


def _fixture_cost() -> ActivationPolicy:
    return live_policy(
        cost={"basis": "fixture", "pricing_source": "synthesis_policy.pricing"}
    )


REJECTED: dict[str, tuple[Callable[[], object], ActivationFailureCode]] = {
    "fixture_policy": (
        lambda: _live(bundled_activation_policy(), counter=FIXTURE_TOKENIZER),
        Code.NOT_APPROVED,
    ),
    "untrusted": (lambda: _live(trust=NoTrustedIssuers()), Code.UNTRUSTED_ISSUER),
    "bad_digest": (lambda: _live(digest="NOT-A-DIGEST"), Code.POLICY_INVALID),
    "budget_binding": (
        lambda: _live(synthesis=_other_synthesis_policy()),
        Code.BUDGET_POLICY_MISMATCH,
    ),
    "tls_disabled": (lambda: _live(_tls_disabled()), Code.TLS_VERIFICATION_DISABLED),
    "policy_env_proxy": (lambda: _live(_proxy_enabled()), Code.PROXY_NOT_ALLOWED),
    "policy_redirects": (lambda: _live(_redirects_enabled()), Code.POLICY_INVALID),
    "corpus_empty": (lambda: _live(_no_corpus()), Code.CORPUS_MISMATCH),
    "fields_prohibited": (
        lambda: _live(_prohibited_without_hash()),
        Code.FIELDS_MISMATCH,
    ),
    "chat_model": (
        lambda: _live(environ=live_environ(EVENTLENS_CHAT_MODEL="gpt-unapproved")),
        Code.MODEL_MISMATCH,
    ),
    "chat_model_missing": (
        lambda: _live(
            environ={
                k: v for k, v in live_environ().items() if k != "EVENTLENS_CHAT_MODEL"
            }
        ),
        Code.MODEL_MISMATCH,
    ),
    "embedding_model": (
        lambda: _live(environ=live_environ(EVENTLENS_EMBEDDING_MODEL="other-embed")),
        Code.MODEL_MISMATCH,
    ),
    "fixture_counter": (
        lambda: _live(counter=FIXTURE_TOKENIZER),
        Code.TOKENIZER_MISMATCH,
    ),
    "endpoint_missing": (
        lambda: _live(
            environ={
                k: v
                for k, v in live_environ().items()
                if k != "EVENTLENS_OPENAI_BASE_URL"
            }
        ),
        Code.ENDPOINT_INVALID,
    ),
    "endpoint_not_allowed": (
        lambda: _live(
            environ=live_environ(EVENTLENS_OPENAI_BASE_URL="https://api.openai.com/v1")
        ),
        Code.ENDPOINT_NOT_ALLOWED,
    ),
    "endpoint_http": (
        lambda: _live(
            environ=live_environ(
                EVENTLENS_OPENAI_BASE_URL="http://provider.example.invalid/v1"
            )
        ),
        Code.INSECURE_SCHEME,
    ),
    "endpoint_credentials": (
        lambda: _live(
            environ=live_environ(
                EVENTLENS_OPENAI_BASE_URL="https://u:pw@provider.example.invalid/v1"
            )
        ),
        Code.ENDPOINT_CREDENTIALS,
    ),
    "endpoint_fragment": (
        lambda: _live(
            environ=live_environ(
                EVENTLENS_OPENAI_BASE_URL="https://provider.example.invalid/v1#x"
            )
        ),
        Code.ENDPOINT_FRAGMENT,
    ),
    "api_key_missing": (
        lambda: _live(
            environ={
                k: v
                for k, v in live_environ().items()
                if k != "EVENTLENS_OPENAI_API_KEY"
            }
        ),
        Code.API_KEY_MISSING,
    ),
    "ambient_https_proxy": (
        lambda: _live(environ=live_environ(HTTPS_PROXY="http://proxy.invalid:3128")),
        Code.PROXY_NOT_ALLOWED,
    ),
    "ambient_lower_proxy": (
        lambda: _live(environ=live_environ(all_proxy="socks5://proxy.invalid")),
        Code.PROXY_NOT_ALLOWED,
    ),
    "ambient_ca_bundle": (
        lambda: _live(environ=live_environ(REQUESTS_CA_BUNDLE="/tmp/evil-ca.pem")),  # noqa: S108
        Code.TLS_VERIFICATION_DISABLED,
    ),
    "ambient_https_verify": (
        lambda: _live(environ=live_environ(PYTHONHTTPSVERIFY="0")),
        Code.TLS_VERIFICATION_DISABLED,
    ),
    "override_verify_flag": (
        lambda: _live(environ=live_environ(EVENTLENS_OPENAI_VERIFY_TLS="false")),
        Code.ENDPOINT_INVALID,
    ),
    "override_vendor_base_url": (
        lambda: _live(environ=live_environ(OPENAI_BASE_URL="https://evil.example")),
        Code.ENDPOINT_INVALID,
    ),
    "cost_fixture_basis": (lambda: _live(_fixture_cost()), Code.COST_POLICY_MISSING),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_every_rejected_activation_makes_zero_outbound_requests(case: str) -> None:
    call, expected = REJECTED[case]
    transport = RecordingTransport([ok(completion())])

    def compose() -> object:
        decision = call()
        # Unreachable on rejection: the adapter is built only after approval.
        return OpenAICompatibleSynthesizer(transport, decision, live_policy())  # pyright: ignore[reportArgumentType]

    with pytest.raises(ProviderActivationError) as caught:
        _ = compose()

    assert caught.value.reason is expected
    assert transport.calls == []
    assert API_KEY_CANARY not in f"{caught.value!s}{caught.value!r}{caught.value.args}"
    assert caught.value.__cause__ is None


def test_fixture_decision_cannot_build_the_http_adapter() -> None:
    decision = activate(
        bundled_activation_policy(),
        bundled_activation_policy_sha256(),
        bundled_synthesis_policy(),
        live_environ(),
        "fixture",
        NoTrustedIssuers(),
        FIXTURE_TOKENIZER,
    )
    transport = RecordingTransport([ok(completion())])

    reason = _reason(
        lambda: OpenAICompatibleSynthesizer(
            transport, decision, bundled_activation_policy()
        )
    )

    assert reason is Code.NOT_APPROVED
    assert transport.calls == []
