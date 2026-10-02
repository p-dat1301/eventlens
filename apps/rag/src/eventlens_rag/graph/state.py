"""Validated LangGraph state and isolated runtime context."""

from dataclasses import dataclass
from typing import ClassVar, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from eventlens_contracts.base import ContractModel
from eventlens_rag.domain.article_document import ArticleDocument


class GraphState(TypedDict):
    """Internal graph state kept per invocation."""

    message: str
    request_id: str


@dataclass(frozen=True, slots=True)
class GraphContext:
    """Invocation-local context passed through LangGraph runtime."""

    request_id: UUID


class GraphInput(BaseModel):
    """Strict external graph input boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)
    message: str = Field(min_length=1, pattern=r".*\S.*")


class GraphOutput(GraphInput):
    """Validated graph output retaining invocation identity."""

    request_id: UUID


class PersistenceState(ContractModel):
    """Validated immutable persistence graph state."""

    article: ArticleDocument
    persisted: bool = False


class PersistenceChannels(TypedDict):
    """Framework channel mapping validated at graph boundaries."""

    article: ArticleDocument
    persisted: bool
