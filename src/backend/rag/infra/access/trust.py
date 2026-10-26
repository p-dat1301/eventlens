"""Trusted issuer registries for live activation (D31).

T6 ships only ``NoTrustedIssuers``: a policy file can never approve itself,
and no live approval exists yet. A real registry is composition-owned.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NoTrustedIssuers:
    """Default registry: approves nothing, so live always fails closed."""

    def approves(self, issuer: str, policy_id: str, policy_sha256: str) -> bool:
        """Never approve."""
        del issuer, policy_id, policy_sha256
        return False
