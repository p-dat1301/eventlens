"""Fail-closed provider activation (D31); framework-free and network-free.

Fixture mode is the default and ignores the environment entirely, so an API
key in the environment can never enable live mode. Live mode is granted only
when every check passes; the first failed check raises
``ProviderActivationError`` before any adapter exists, so a rejected
configuration makes zero outbound requests.

Live checks, in order (each reason is independently reachable):

1. requested mode equals the policy mode (a fixture policy approves nothing)
2. the policy digest is a lowercase sha256 hex string
3. a composition-owned ``TrustedIssuerRegistry`` approves issuer + policy ID +
   digest (the policy file never approves itself)
4. budget binding names the loaded synthesis policy (single numeric source)
5. transport flags: TLS verification on, no env proxy, no redirects/retries
6. corpus and the five allowlisted source field groups
7. environment chat/embedding model IDs equal the bindings
8. the runtime token counter equals the approved chat tokenizer
9. the base URL canonicalizes and exactly matches one allowlisted endpoint
10. an API key is present (config only, never a permission)
11. an approved live cost basis exists (fixture rates are never live prices)

Environment values are configuration checked by the policy; none of them
grants permission. Ambient proxy and TLS override variables are rejected for
live mode by ``reject_ambient_overrides`` so they cannot bypass the endpoint
allowlist.
"""

import re
from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import SplitResult, urlsplit

from eventlens_rag.domain.activation import (
    ALLOWED_SOURCE_FIELDS,
    ActivationDecision,
    ActivationPolicy,
    ApiKey,
    CanonicalEndpoint,
    EndpointRule,
    ProviderEnvironment,
)
from eventlens_rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
)
from eventlens_rag.domain.synthesis_policy import SynthesisPolicy
from eventlens_rag.ports.synthesis import TrustedIssuerRegistry
from eventlens_rag.ports.tokens import TokenizerIdentity

ENV_BASE_URL: Final = "EVENTLENS_OPENAI_BASE_URL"
ENV_API_KEY: Final = "EVENTLENS_OPENAI_API_KEY"
ENV_CHAT_MODEL: Final = "EVENTLENS_CHAT_MODEL"
ENV_EMBEDDING_MODEL: Final = "EVENTLENS_EMBEDDING_MODEL"

DEFAULT_HTTPS_PORT: Final = 443
MAX_PORT: Final = 65_535

REQUIRED_PROHIBITED: Final = frozenset(
    {
        "canonical_url",
        "raw_storage_uri",
        "raw_content",
        "content_hash",
        "span_fingerprint",
        "processing_metadata",
        "enrichment_metadata",
        "credentials",
        "caller_scope",
    }
)

# Ambient variables that could route traffic around the endpoint allowlist.
PROXY_VARIABLES: Final = frozenset(
    {"https_proxy", "http_proxy", "all_proxy", "no_proxy"}
)
# Ambient variables that could weaken or replace TLS verification.
TLS_OVERRIDE_VARIABLES: Final = frozenset(
    {
        "pythonhttpsverify",
        "ssl_cert_file",
        "ssl_cert_dir",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "httpx_ca_bundle",
    }
)
# Only the four plan variables are recognized under the provider prefixes.
_RECOGNIZED: Final = frozenset(
    {ENV_BASE_URL, ENV_API_KEY, ENV_CHAT_MODEL, ENV_EMBEDDING_MODEL}
)
_PROVIDER_PREFIXES: Final = ("EVENTLENS_OPENAI_", "OPENAI_")

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_PORT = re.compile(r"[0-9]{1,5}")
_MAX_HOST_LENGTH: Final = 253


def _reject(reason: ActivationFailureCode) -> ProviderActivationError:
    return ProviderActivationError(reason)


def _value(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def read_provider_environment(environ: Mapping[str, str]) -> ProviderEnvironment:
    """Read exactly the four plan variables; nothing here grants permission."""
    key = _value(environ, ENV_API_KEY)
    return ProviderEnvironment(
        base_url=_value(environ, ENV_BASE_URL),
        api_key=None if key is None else ApiKey(key),
        chat_model=_value(environ, ENV_CHAT_MODEL),
        embedding_model=_value(environ, ENV_EMBEDDING_MODEL),
    )


def reject_ambient_overrides(environ: Mapping[str, str]) -> None:
    """Fail closed for live mode when ambient env could bypass the policy.

    Proxy variables (any case) could route around the exact endpoint
    allowlist, TLS/CA overrides could weaken verification, and any other
    provider-prefixed variable is an unapproved override (for example an
    alternate base URL, organization or verify flag).
    """
    for name, value in environ.items():
        if not value.strip():
            continue
        lowered = name.lower()
        if lowered in PROXY_VARIABLES:
            raise _reject(ActivationFailureCode.PROXY_NOT_ALLOWED)
        if lowered in TLS_OVERRIDE_VARIABLES:
            raise _reject(ActivationFailureCode.TLS_VERIFICATION_DISABLED)
        if name.upper().startswith(_PROVIDER_PREFIXES) and name not in _RECOGNIZED:
            raise _reject(ActivationFailureCode.ENDPOINT_INVALID)


def _split(url: str) -> SplitResult:
    if not url or url != url.strip() or not url.isascii() or "\\" in url:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in url):  # noqa: PLR2004
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    try:
        return urlsplit(url)
    except ValueError:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID) from None


def _port(raw: str) -> int:
    if raw == "":
        return DEFAULT_HTTPS_PORT
    if _PORT.fullmatch(raw) is None:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    port = int(raw)
    if not 1 <= port <= MAX_PORT:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    return port


def _base_path(path: str) -> str:
    trimmed = path.removesuffix("/")
    if trimmed == "":
        return ""
    segments = trimmed.split("/")
    if segments[0] != "":
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    for segment in segments[1:]:
        if segment in {"", ".", ".."} or _SEGMENT.fullmatch(segment) is None:
            raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    return trimmed


def canonicalize_endpoint(url: str) -> CanonicalEndpoint:
    """Parse one exact HTTPS endpoint or raise a typed activation error.

    Canonical form: lowercase host, explicit port (default 443), base path
    without trailing slash. Credentials, fragments (even empty), queries,
    percent-encoding, dot segments, IP literals, trailing-dot hosts and
    non-ASCII hosts are rejected. Nothing is resolved or fetched.
    """
    if "#" in url:
        raise _reject(ActivationFailureCode.ENDPOINT_FRAGMENT)
    parts = _split(url)
    scheme = parts.scheme.lower()
    if scheme == "http":
        raise _reject(ActivationFailureCode.INSECURE_SCHEME)
    if scheme != "https" or not url.lower().startswith("https://"):
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    netloc = parts.netloc
    if "@" in netloc:
        raise _reject(ActivationFailureCode.ENDPOINT_CREDENTIALS)
    if "?" in url:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    host, separator, raw_port = netloc.partition(":")
    if ":" in raw_port or "[" in netloc or (separator and not raw_port):
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    host = host.lower()
    if len(host) > _MAX_HOST_LENGTH or _HOST.fullmatch(host) is None:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    if host.rsplit(".", 1)[-1].isdigit():
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    return CanonicalEndpoint(
        scheme="https",
        host=host,
        port=_port(raw_port),
        base_path=_base_path(parts.path),
    )


def endpoint_allowed(
    endpoint: CanonicalEndpoint, rules: tuple[EndpointRule, ...]
) -> bool:
    """Exact tuple equality on scheme/host/port/base path; no wildcard."""
    return any(
        (rule.scheme, rule.host, rule.port, rule.base_path)
        == (endpoint.scheme, endpoint.host, endpoint.port, endpoint.base_path)
        for rule in rules
    )


def _fixture_decision(policy: ActivationPolicy) -> ActivationDecision:
    return ActivationDecision(
        mode="fixture",
        policy_id=policy.policy_id,
        issuer=policy.issuer,
        chat_model=policy.chat_model,
        embedding_model=policy.embedding_model,
        endpoint=None,
        api_key=None,
    )


def _check_trust(
    policy: ActivationPolicy, policy_sha256: str, trust: TrustedIssuerRegistry
) -> None:
    if _SHA256_HEX.fullmatch(policy_sha256) is None:
        raise _reject(ActivationFailureCode.POLICY_INVALID)
    if trust.approves(policy.issuer, policy.policy_id, policy_sha256) is not True:
        raise _reject(ActivationFailureCode.UNTRUSTED_ISSUER)


def _check_transport(policy: ActivationPolicy) -> None:
    transport = policy.transport
    if transport.tls_verify is not True:
        raise _reject(ActivationFailureCode.TLS_VERIFICATION_DISABLED)
    if transport.trust_env_proxy is not False:
        raise _reject(ActivationFailureCode.PROXY_NOT_ALLOWED)
    if transport.follow_redirects is not False or transport.client_retries != 0:
        raise _reject(ActivationFailureCode.POLICY_INVALID)


def _check_corpus(policy: ActivationPolicy) -> None:
    corpus = policy.corpus
    if not corpus.corpus_ids or len(set(corpus.corpus_ids)) != len(corpus.corpus_ids):
        raise _reject(ActivationFailureCode.CORPUS_MISMATCH)
    if corpus.data_classification != "internal":
        raise _reject(ActivationFailureCode.CORPUS_MISMATCH)
    if corpus.allowed_source_fields != ALLOWED_SOURCE_FIELDS:
        raise _reject(ActivationFailureCode.FIELDS_MISMATCH)
    if not REQUIRED_PROHIBITED.issubset(corpus.prohibited):
        raise _reject(ActivationFailureCode.FIELDS_MISMATCH)


def _check_models(
    policy: ActivationPolicy,
    environment: ProviderEnvironment,
    counter_identity: TokenizerIdentity,
) -> None:
    if environment.chat_model != policy.chat_model.model_id:
        raise _reject(ActivationFailureCode.MODEL_MISMATCH)
    if environment.embedding_model != policy.embedding_model.model_id:
        raise _reject(ActivationFailureCode.MODEL_MISMATCH)
    chat = policy.chat_model
    if chat.tokenizer_basis == "fixture" or (
        counter_identity.tokenizer_id,
        counter_identity.version,
    ) != (chat.tokenizer_id, chat.tokenizer_version):
        raise _reject(ActivationFailureCode.TOKENIZER_MISMATCH)


def _check_endpoint(
    policy: ActivationPolicy, environment: ProviderEnvironment
) -> CanonicalEndpoint:
    if environment.base_url is None:
        raise _reject(ActivationFailureCode.ENDPOINT_INVALID)
    endpoint = canonicalize_endpoint(environment.base_url)
    if not endpoint_allowed(endpoint, policy.endpoints):
        raise _reject(ActivationFailureCode.ENDPOINT_NOT_ALLOWED)
    return endpoint


def _check_cost(policy: ActivationPolicy, synthesis: SynthesisPolicy) -> None:
    # Fixture rates are never live prices: an approved live basis needs a
    # pricing source that is itself approved for live use. synthesis-policy.v1
    # only carries fixture pricing, so no live run can pass this check in T6.
    pricing_basis = str(synthesis.pricing.basis)
    if policy.cost.basis != "approved_live" or pricing_basis != "approved_live":
        raise _reject(ActivationFailureCode.COST_POLICY_MISSING)


def evaluate_activation(  # noqa: PLR0913, PLR0917 - frozen signature
    policy: ActivationPolicy,
    policy_sha256: str,
    synthesis: SynthesisPolicy,
    environment: ProviderEnvironment,
    requested_mode: Literal["fixture", "live"],
    trust: TrustedIssuerRegistry,
    counter_identity: TokenizerIdentity,
) -> ActivationDecision:
    """Return the fixture decision, or a live decision only if all checks pass.

    Fixture: the environment (including any API key) is ignored; the policy
    must itself be a fixture policy bound to the loaded synthesis policy.
    Live: fail closed with ``ProviderActivationError`` on the first mismatch.
    """
    if requested_mode == "fixture":
        if policy.mode != "fixture":
            raise _reject(ActivationFailureCode.POLICY_INVALID)
        if policy.budget.synthesis_policy_id != synthesis.policy_id:
            raise _reject(ActivationFailureCode.BUDGET_POLICY_MISMATCH)
        return _fixture_decision(policy)
    if policy.mode != "live":
        raise _reject(ActivationFailureCode.NOT_APPROVED)
    _check_trust(policy, policy_sha256, trust)
    if policy.budget.synthesis_policy_id != synthesis.policy_id:
        raise _reject(ActivationFailureCode.BUDGET_POLICY_MISMATCH)
    _check_transport(policy)
    _check_corpus(policy)
    _check_models(policy, environment, counter_identity)
    endpoint = _check_endpoint(policy, environment)
    if environment.api_key is None or not environment.api_key.value:
        raise _reject(ActivationFailureCode.API_KEY_MISSING)
    _check_cost(policy, synthesis)
    return ActivationDecision(
        mode="live",
        policy_id=policy.policy_id,
        issuer=policy.issuer,
        chat_model=policy.chat_model,
        embedding_model=policy.embedding_model,
        endpoint=endpoint,
        api_key=environment.api_key,
    )


def activate(  # noqa: PLR0913, PLR0917 - frozen signature
    policy: ActivationPolicy,
    policy_sha256: str,
    synthesis: SynthesisPolicy,
    environ: Mapping[str, str],
    requested_mode: Literal["fixture", "live"],
    trust: TrustedIssuerRegistry,
    counter_identity: TokenizerIdentity,
) -> ActivationDecision:
    """Composition helper: read env, reject ambient overrides for live, decide."""
    if requested_mode == "live":
        reject_ambient_overrides(environ)
    return evaluate_activation(
        policy,
        policy_sha256,
        synthesis,
        read_provider_environment(environ),
        requested_mode,
        trust,
        counter_identity,
    )
