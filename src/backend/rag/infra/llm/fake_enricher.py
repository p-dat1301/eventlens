"""Deterministic local enrichment adapters; no network or credentials."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from backend.rag.application.ports.enrichment import (
    EnrichmentProviderError,
    EnrichmentRequest,
)
from backend.rag.domain.enrichment_draft import (
    DraftEvidence,
    DraftFieldConfidence,
    EnrichmentDraft,
)
from backend.rag.domain.enrichment_lifecycle import FailureCode

FAKE_MODEL_VERSION: Final = "fake-enricher.v1"
FAKE_PROMPT_VERSION: Final = "enrichment-prompt.v1"

# Keyword -> controlled topic. Article text is only scanned as data: any
# instruction-like text inside it has no effect on the output shape.
KEYWORDS: Final = (
    ("harbor", "transportation"),
    ("flight", "transportation"),
    ("traffic", "transportation"),
    ("storm", "weather"),
    ("flood", "weather"),
    ("power", "energy"),
    ("grid", "energy"),
    ("hospital", "health"),
    ("election", "politics"),
    ("market", "economy"),
    ("fire", "public_safety"),
    ("software", "technology"),
)


@dataclass(frozen=True, slots=True)
class FakeArticleEnricher:
    """Map keyword hits to controlled topics with exact evidence spans."""

    model_version: str = FAKE_MODEL_VERSION
    prompt_version: str = FAKE_PROMPT_VERSION

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Return one deterministic draft for the allowlisted context."""
        content = request.context.normalized_content
        topics: list[str] = []
        evidence: list[DraftEvidence] = []
        for keyword, topic in KEYWORDS:
            # Search the original text case-insensitively, character by
            # character: unlike casefold() this never changes string length, so
            # match offsets are code points of normalized_content itself.
            match = re.search(re.escape(keyword), content, re.IGNORECASE)
            if match is None or topic in topics:
                continue
            start, end = match.span()
            topics.append(topic)
            evidence.append(
                DraftEvidence(
                    start_offset=start, end_offset=end, excerpt=content[start:end]
                )
            )
        return EnrichmentDraft(
            topics=tuple(topics) or ("other",),
            content_type="news_report",
            event_relevance="uncertain",
            entities=(),
            locations=(),
            suggested_tags=(),
            evidence=tuple(evidence),
            field_confidence=(DraftFieldConfidence(field="topics", confidence=0.6),),
        )


type Script = (
    EnrichmentDraft
    | EnrichmentProviderError
    | Callable[[EnrichmentRequest], EnrichmentDraft]
)


@dataclass(slots=True)
class ScriptedArticleEnricher:  # noqa: MUTABLE_OK
    """Replay scripted drafts/errors in order and record every request."""

    script: list[Script]
    model_version: str = FAKE_MODEL_VERSION
    prompt_version: str = FAKE_PROMPT_VERSION
    requests: list[EnrichmentRequest] = field(default_factory=list)

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Return or raise the next scripted step."""
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, EnrichmentProviderError):
            raise step
        if isinstance(step, EnrichmentDraft):
            return step
        return step(request)


@dataclass(frozen=True, slots=True)
class FakeTimeoutEnricher:
    """Fault injection: same identity as the fake, every call times out."""

    model_version: str = FakeArticleEnricher().model_version
    prompt_version: str = FakeArticleEnricher().prompt_version

    def enrich(self, request: EnrichmentRequest) -> EnrichmentDraft:
        """Simulate a provider deadline expiry without sleeping."""
        del request
        raise EnrichmentProviderError(FailureCode.PROVIDER_TIMEOUT)
