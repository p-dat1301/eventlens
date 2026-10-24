"""Exact HTTPS endpoint canonicalization and allowlist matching (D31)."""

import pytest
from provider_support import (  # pyright: ignore[reportImplicitRelativeImport]
    forbid_network,
    live_policy,
)

from eventlens_rag.application.activation import (
    canonicalize_endpoint,
    endpoint_allowed,
)
from eventlens_rag.domain.activation import CanonicalEndpoint
from eventlens_rag.domain.synthesis_errors import (
    ActivationFailureCode,
    ProviderActivationError,
)

Code = ActivationFailureCode
ALLOWED = CanonicalEndpoint("https", "provider.example.invalid", 443, "/v1")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_network(monkeypatch)


@pytest.mark.parametrize(
    "url",
    [
        "https://provider.example.invalid/v1",
        "https://provider.example.invalid/v1/",
        "https://provider.example.invalid:443/v1",
        "HTTPS://Provider.Example.INVALID/v1",
    ],
)
def test_equivalent_spellings_canonicalize_to_the_allowed_endpoint(url: str) -> None:
    endpoint = canonicalize_endpoint(url)

    assert endpoint == ALLOWED
    assert endpoint_allowed(endpoint, live_policy().endpoints)
    assert endpoint.url("/chat/completions") == (
        "https://provider.example.invalid:443/v1/chat/completions"
    )


def test_root_path_and_explicit_port_are_kept_exact() -> None:
    endpoint = canonicalize_endpoint("https://api.example.invalid:8443")

    assert endpoint == CanonicalEndpoint("https", "api.example.invalid", 8443, "")


@pytest.mark.parametrize(
    "url",
    [
        "https://provider.example.invalid.evil.test/v1",
        "https://evil-provider.example.invalid/v1",
        "https://sub.provider.example.invalid/v1",
        "https://example.invalid/v1",
        "https://provider.example.invalid:8443/v1",
        "https://provider.example.invalid/v1beta",
        "https://provider.example.invalid/v1/extra",
        "https://provider.example.invalid/V1",
        "https://provider.example.invalid",
    ],
)
def test_no_wildcard_substring_port_or_path_prefix_match(url: str) -> None:
    endpoint = canonicalize_endpoint(url)

    assert not endpoint_allowed(endpoint, live_policy().endpoints)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://provider.example.invalid/v1", Code.INSECURE_SCHEME),
        ("ftp://provider.example.invalid/v1", Code.ENDPOINT_INVALID),
        ("provider.example.invalid/v1", Code.ENDPOINT_INVALID),
        ("https://user:pw@provider.example.invalid/v1", Code.ENDPOINT_CREDENTIALS),
        ("https://user@provider.example.invalid/v1", Code.ENDPOINT_CREDENTIALS),
        ("https://@provider.example.invalid/v1", Code.ENDPOINT_CREDENTIALS),
        ("https://provider.example.invalid/v1#frag", Code.ENDPOINT_FRAGMENT),
        ("https://provider.example.invalid/v1#", Code.ENDPOINT_FRAGMENT),
        ("https://provider.example.invalid/v1?api-version=x", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/v1?", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/../v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/v1/./", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid//v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/v%31", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid./v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid:0/v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid:65536/v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid:44a/v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid:/v1", Code.ENDPOINT_INVALID),
        ("https://127.0.0.1/v1", Code.ENDPOINT_INVALID),
        ("https://[::1]/v1", Code.ENDPOINT_INVALID),
        ("https://localhost/v1", Code.ENDPOINT_INVALID),
        ("https://*.example.invalid/v1", Code.ENDPOINT_INVALID),
        ("https://prövider.example.invalid/v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid\\@evil.test/v1", Code.ENDPOINT_INVALID),
        (" https://provider.example.invalid/v1", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/v1\n", Code.ENDPOINT_INVALID),
        ("https://provider.example.invalid/v 1", Code.ENDPOINT_INVALID),
        ("", Code.ENDPOINT_INVALID),
    ],
)
def test_malformed_or_unsafe_urls_raise_typed_codes(
    url: str, expected: ActivationFailureCode
) -> None:
    with pytest.raises(ProviderActivationError) as caught:
        _ = canonicalize_endpoint(url)

    assert caught.value.reason is expected
    assert "pw" not in str(caught.value)
    assert caught.value.__cause__ is None
