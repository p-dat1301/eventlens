"""Local fixture policy; not production authentication."""

from dataclasses import dataclass

from eventlens_rag.domain.access import CallerScope


@dataclass(frozen=True, slots=True)
class FixtureAccessPolicy:
    """Allow only the current local fixture principal and policy."""

    revoked: bool = False

    def allows(self, scope: CallerScope) -> bool:
        """Evaluate current fixture policy without widening shared access."""
        return (
            not self.revoked
            and scope.principal_id == "fixture-analyst"
            and scope.scope_id == "fixture-internal"
            and scope.policy_id == "fixture-policy"
            and scope.policy_version == "v1"
        )
