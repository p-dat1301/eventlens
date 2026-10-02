"""Framework-free caller authorization boundary."""

from typing import Protocol

from eventlens_rag.domain.access import CallerScope


class AccessPolicy(Protocol):
    """Check current policy before persistence or source access."""

    def allows(self, scope: CallerScope) -> bool:
        """Evaluate current caller authorization."""
        ...
