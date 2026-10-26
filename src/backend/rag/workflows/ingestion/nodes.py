# ruff: noqa: EM101

"""Ingestion nodes: read state, call one ``EnrichmentSteps`` method, return a patch."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from langgraph.runtime import Runtime

from backend.rag.application.use_cases.enrichment import EnrichmentSteps, StepFailure
from backend.rag.application.use_cases.index import StagedGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.enrichment_draft import EnrichmentDraft
from backend.rag.domain.enrichment_lifecycle import ClaimedJob, FailureCode, JobOutcome
from backend.rag.workflows.ingestion.context import EnrichmentContext
from backend.rag.workflows.ingestion.state import EnrichmentChannels
from backend.shared.contracts import ArticleEnrichment

type Node = Callable[
    [EnrichmentChannels, Runtime[EnrichmentContext]], EnrichmentChannels
]


class GraphStateError(RuntimeError):
    """A node ran without the channel its predecessor must have produced."""

    def __init__(self, channel: str) -> None:
        """Name the missing channel."""
        super().__init__(f"enrichment graph channel missing: {channel}")


def _claim(state: EnrichmentChannels) -> ClaimedJob:
    value = state.get("claim")
    if value is None:
        raise GraphStateError("claim")
    return value


def _article(state: EnrichmentChannels) -> ArticleDocument:
    value = state.get("article")
    if value is None:
        raise GraphStateError("article")
    return value


def _draft(state: EnrichmentChannels) -> EnrichmentDraft:
    value = state.get("draft")
    if value is None:
        raise GraphStateError("draft")
    return value


def _metadata(state: EnrichmentChannels) -> ArticleEnrichment:
    value = state.get("metadata")
    if value is None:
        raise GraphStateError("metadata")
    return value


def _staged(state: EnrichmentChannels) -> StagedGeneration:
    value = state.get("staged")
    if value is None:
        raise GraphStateError("staged")
    return value


def _failure(state: EnrichmentChannels) -> FailureCode:
    value = state.get("failure")
    if value is None:
        raise GraphStateError("failure")
    return value


def _patch(
    result: object, success_key: Literal["article", "draft", "metadata", "staged"]
) -> EnrichmentChannels:
    if isinstance(result, JobOutcome):
        return {"outcome": result}
    if isinstance(result, StepFailure):
        return {"failure": result.code}
    update: EnrichmentChannels = {}
    match success_key:
        case "article":
            assert isinstance(result, ArticleDocument)  # noqa: S101
            update["article"] = result
        case "draft":
            assert isinstance(result, EnrichmentDraft)  # noqa: S101
            update["draft"] = result
        case "metadata":
            assert isinstance(result, ArticleEnrichment)  # noqa: S101
            update["metadata"] = result
        case "staged":
            assert isinstance(result, StagedGeneration)  # noqa: S101
            update["staged"] = result
    return update


@dataclass(frozen=True, slots=True)
class EnrichmentNodes:
    """Node callables bound to the injected application steps."""

    steps: EnrichmentSteps

    def load(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Load the claimed article revision."""
        return _patch(self.steps.load(_claim(state), runtime.context.scope), "article")

    def enrich(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Ask the enricher for a metadata draft."""
        result = self.steps.enrich(
            _claim(state), _article(state), runtime.context.scope
        )
        return _patch(result, "draft")

    def validate(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Validate the draft into versioned metadata."""
        result = self.steps.validate(
            _claim(state), _article(state), _draft(state), runtime.context.scope
        )
        return _patch(result, "metadata")

    def stage(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Embed and stage an inactive index generation."""
        result = self.steps.stage(_claim(state), _article(state), runtime.context.scope)
        return _patch(result, "staged")

    def publish(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Publish metadata and generation under the claim fence."""
        outcome = self.steps.publish(
            _claim(state), _staged(state), _metadata(state), runtime.context.scope
        )
        return {"outcome": outcome}

    def fail(
        self, state: EnrichmentChannels, runtime: Runtime[EnrichmentContext]
    ) -> EnrichmentChannels:
        """Record one fenced retry or FAILED write."""
        outcome = self.steps.fail(_claim(state), _failure(state), runtime.context.scope)
        return {"outcome": outcome}
