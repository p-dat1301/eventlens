"""Framework-free API use cases."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Service readiness result."""

    status: Literal["ok"]


def get_health_status() -> HealthStatus:
    """Report service readiness without framework dependencies."""
    return HealthStatus(status="ok")
