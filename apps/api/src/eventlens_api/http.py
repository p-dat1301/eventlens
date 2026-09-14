"""FastAPI adapter for API use cases."""

from typing import ClassVar, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from eventlens_api.application import get_health_status

router = APIRouter()


class HealthResponse(BaseModel):
    """HTTP representation of readiness."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    status: Literal["ok"]


@router.get("/health")
def health() -> HealthResponse:
    """Adapt readiness result to HTTP response."""
    result = get_health_status()
    return HealthResponse(status=result.status)
