"""Framework-free caller authorization boundary."""

from typing import Protocol

from backend.rag.domain.access import CallerScope


class AccessPolicy(Protocol):
    """Check current policy before persistence or source access."""

    def allows(self, scope: CallerScope) -> bool:
        """Evaluate current caller authorization."""
        ...
