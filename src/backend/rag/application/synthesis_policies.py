"""Bundled T6 policy loaders (frozen, shared by runtime and tests)."""

from hashlib import sha256
from importlib.resources import files
from typing import Final

from backend.rag.domain.activation import ActivationPolicy
from backend.rag.domain.synthesis_policy import SynthesisPolicy

SYNTHESIS_POLICY_RESOURCE: Final = "synthesis-policy.v1.json"
ACTIVATION_POLICY_RESOURCE: Final = "provider-activation.fixture.v1.json"


def _policy_bytes(resource: str) -> bytes:
    return (
        files("backend.rag.application")
        .joinpath("policies")
        .joinpath(resource)
        .read_bytes()
    )


def bundled_synthesis_policy() -> SynthesisPolicy:
    """Load the frozen synthesis/budget policy shipped with the package."""
    return SynthesisPolicy.model_validate_json(_policy_bytes(SYNTHESIS_POLICY_RESOURCE))


def bundled_activation_policy() -> ActivationPolicy:
    """Load the frozen fixture activation policy shipped with the package."""
    return ActivationPolicy.model_validate_json(
        _policy_bytes(ACTIVATION_POLICY_RESOURCE)
    )


def bundled_activation_policy_sha256() -> str:
    """Hex digest of the exact bundled activation artifact bytes."""
    return sha256(_policy_bytes(ACTIVATION_POLICY_RESOURCE)).hexdigest()
