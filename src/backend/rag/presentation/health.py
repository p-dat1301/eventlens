"""rag-api readiness route; independent of the CRUD context."""

from typing import ClassVar, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class HealthResponse(BaseModel):
    """HTTP representation of rag-api readiness."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    status: Literal["ok"]


@router.get("/health")
def health() -> HealthResponse:
    """Report that the rag-api process is serving requests."""
    return HealthResponse(status="ok")
