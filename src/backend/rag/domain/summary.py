"""Per-revision AI summary: draft contract, validation and failure codes.

A summary covers exactly one ``ArticleDocument`` revision. The model returns
``article_summary_draft.v1``: a short paragraph plus 1-5 key points, each
quoting 1-3 verbatim passages of the NFC content. The server locates every
quote (first exact occurrence) and stores Unicode code-point offsets plus a
fingerprint; a quote that is not an exact substring rejects the whole draft
(no repair, no fuzzy match). Offsets and hashes prove that a span exists,
not that it semantically supports the point.

Articles longer than ``MAX_INPUT_CHARS`` are refused (``content_too_long``)
instead of silently truncated.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, ClassVar, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from backend.rag.domain.catalog import SummaryKeyPoint, SupportingSpan

SUMMARY_SCHEMA_VERSION: Final = "article_summary_draft.v1"
SUMMARY_POLICY_ID: Final = "article-summary-policy.v1"
MAX_INPUT_CHARS: Final = 24_000
MAX_OUTPUT_TOKENS: Final = 900
CALL_TIMEOUT_SECONDS: Final = 60.0
MAX_SUMMARY_CHARS: Final = 1_200
MAX_POINT_CHARS: Final = 400


class SummaryFailureCode(StrEnum):
    """Stable summary job failure codes."""

    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    MALFORMED_OUTPUT = "malformed_output"
    INVALID_EVIDENCE = "invalid_evidence"
    CONTENT_TOO_LONG = "content_too_long"
    REVISION_MISSING = "revision_missing"

    @property
    def retryable(self) -> bool:
        """Only transient provider failures are retried."""
        return self in {
            SummaryFailureCode.PROVIDER_TIMEOUT,
            SummaryFailureCode.PROVIDER_UNAVAILABLE,
        }


class SummaryProviderError(RuntimeError):
    """Typed provider failure (code only; no body, prompt or key)."""

    def __init__(self, code: SummaryFailureCode) -> None:
        """Keep the stable code."""
        super().__init__(code.value)
        self.code: SummaryFailureCode = code


Text = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


MAX_QUOTE_CHARS: Final = 600


class EvidenceQuote(_Strict):
    """Verbatim passage of the article content."""

    quote: Annotated[
        str, StringConstraints(min_length=8, max_length=MAX_QUOTE_CHARS, pattern=r"\S")
    ]


class KeyPointDraft(_Strict):
    """One key point with its supporting quotes."""

    text: Annotated[Text, StringConstraints(max_length=MAX_POINT_CHARS)]
    evidence: Annotated[tuple[EvidenceQuote, ...], Field(min_length=1, max_length=3)]


class SummaryDraft(_Strict):
    """Strict model output ``article_summary_draft.v1``."""

    summary: Annotated[Text, StringConstraints(max_length=MAX_SUMMARY_CHARS)]
    key_points: Annotated[tuple[KeyPointDraft, ...], Field(min_length=1, max_length=5)]


def parse_draft(content: str) -> SummaryDraft:
    """Strict JSON parse; ``SummaryProviderError(MALFORMED_OUTPUT)`` otherwise."""
    try:
        return SummaryDraft.model_validate_json(content)
    except ValidationError:
        raise SummaryProviderError(SummaryFailureCode.MALFORMED_OUTPUT) from None


def span_fingerprint(excerpt: str) -> str:
    """SHA-256 identity of an excerpt (same form as citation spans)."""
    return f"sha256:{sha256(excerpt.encode()).hexdigest()}"


def validate_draft(draft: SummaryDraft, content: str) -> tuple[SummaryKeyPoint, ...]:
    """Bind every quote to its first exact offsets in ``content`` or reject."""
    points: list[SummaryKeyPoint] = []
    for point in draft.key_points:
        spans: list[SupportingSpan] = []
        for quote in point.evidence:
            start = content.find(quote.quote)
            if start < 0:
                raise SummaryProviderError(SummaryFailureCode.INVALID_EVIDENCE)
            end = start + len(quote.quote)
            excerpt = content[start:end]
            spans.append(SupportingSpan(start, end, excerpt, span_fingerprint(excerpt)))
        points.append(SummaryKeyPoint(point.text, tuple(spans)))
    return tuple(points)


def draft_json_schema() -> dict[str, object]:
    """JSON Schema sent as the structured-output contract."""
    return SummaryDraft.model_json_schema()


@dataclass(frozen=True, slots=True)
class SummaryModelIdentity:
    """Model call identity that keys a stored summary."""

    model_id: str
    model_version: str
    prompt_version: str


@dataclass(frozen=True, slots=True)
class SummarySource:
    """The exact revision to summarize (allowlisted fields only)."""

    article_id: UUID
    content_hash: str
    title: str
    content: str
    source_name: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """Validated summary ready for a fenced publication."""

    text: str
    key_points: tuple[SummaryKeyPoint, ...]
    input_chars: int
    usage: dict[str, int] | None


def key_points_json(points: tuple[SummaryKeyPoint, ...]) -> list[dict[str, object]]:
    """Storage form of key points."""
    return [
        {
            "text": p.text,
            "spans": [
                {
                    "start_offset": s.start_offset,
                    "end_offset": s.end_offset,
                    "excerpt": s.excerpt,
                    "span_fingerprint": s.span_fingerprint,
                }
                for s in p.spans
            ],
        }
        for p in points
    ]


type SummaryStatusName = Literal["published", "stale", "lost", "retry", "failed"]


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    """Terminal result of one claimed summary job."""

    job_id: UUID
    article_id: UUID
    status: SummaryStatusName
    code: str | None = None
