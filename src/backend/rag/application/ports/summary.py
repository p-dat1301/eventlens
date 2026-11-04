"""Article summary ports: the model and the fenced summary repository."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from backend.rag.domain.summary import (
    SummaryModelIdentity,
    SummaryResult,
    SummarySource,
)
from backend.rag.domain.work_jobs import JobFence


@dataclass(frozen=True, slots=True)
class SummaryModelRequest:
    """One model call over exactly one revision."""

    source: SummarySource
    timeout_seconds: float
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class SummaryModelResponse:
    """Untrusted model content; parsed strictly by the application."""

    content: str
    usage: dict[str, int] | None
    reported_model: str | None


class ArticleSummaryModelPort(Protocol):
    """One call per ``summarize``; raises only ``SummaryProviderError``."""

    @property
    def identity(self) -> SummaryModelIdentity:
        """Configured model/prompt identity."""
        ...

    def summarize(self, request: SummaryModelRequest) -> SummaryModelResponse:
        """Return the structured draft text or raise a typed error."""
        ...


type PublishResult = Literal["published", "stale", "lost"]


class SummaryRepository(Protocol):
    """Exact-revision reads and the fenced publication."""

    def load(self, article_id: UUID, content_hash: str) -> SummarySource | None:
        """The revision itself (``None`` if it no longer exists)."""
        ...

    def is_current(self, article_id: UUID, content_hash: str) -> bool:
        """Whether the revision is still current and not tombstoned."""
        ...

    def publish(  # noqa: PLR0913 - one fenced write needs every identity part
        self,
        *,
        fence: JobFence,
        source: SummarySource,
        identity: SummaryModelIdentity,
        policy_id: str,
        schema_version: str,
        result: SummaryResult,
    ) -> PublishResult:
        """Insert the summary and mark the job DONE in one transaction.

        ``stale`` when the revision stopped being current (job STALE, nothing
        published); ``lost`` when the claim fence no longer holds.
        """
        ...
