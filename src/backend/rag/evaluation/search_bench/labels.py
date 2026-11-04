"""Question/gold format ``search-questions.v1`` (committable, no article text).

Gold is recorded at two levels: graded relevant articles (2 = a passage answers
the question, 1 = related/partial) and, for each article, exact passage spans
as NFC code-point offsets ``[start, end)`` of the revision named by
``content_hash``, plus the SHA-256 of the excerpt so a reviewer can prove the
offsets still point at the same text without the text being stored here.

``label_status`` separates machine-proposed candidates from human-reviewed
labels; only ``reviewed_*`` questions may support a final decision.
"""

from hashlib import sha256
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from backend.shared.contracts.base import (
    ContractModel,
    NonEmptyText,
    PrefixedSha256,
    Sha256,
    UtcDatetime,
)

Category = Literal[
    "proper_noun",
    "datetime",
    "paraphrase",
    "filter",
    "multi_source",
    "near_duplicate",
    "no_answer",
]
LabelStatus = Literal[
    "candidate", "reviewed_accepted", "reviewed_edited", "reviewed_rejected"
]
REVIEWED: frozenset[str] = frozenset({"reviewed_accepted", "reviewed_edited"})


def excerpt_digest(text: str) -> str:
    """SHA-256 hex of an NFC excerpt (UTF-8)."""
    return sha256(text.encode()).hexdigest()


class GoldSpan(ContractModel):
    """Exact answer passage inside one revision."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    excerpt_sha256: Sha256

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """Non-empty half-open range."""
        if self.end <= self.start:
            msg = "span end must be greater than start"
            raise ValueError(msg)
        return self


class GoldArticle(ContractModel):
    """One graded relevant article revision."""

    article_id: UUID
    content_hash: PrefixedSha256
    grade: Literal[1, 2]
    spans: tuple[GoldSpan, ...]


class QuestionFilters(ContractModel):
    """Same semantics as ``RetrievalScope`` (after inclusive, before exclusive)."""

    source_domains: tuple[str, ...] = ()
    published_after: UtcDatetime | None = None
    published_before: UtcDatetime | None = None


class SearchQuestion(ContractModel):
    """One benchmark question with its split and review state."""

    qid: NonEmptyText
    question: NonEmptyText
    category: Category
    split: Literal["dev", "test"]
    answerable: bool
    filters: QuestionFilters
    gold: tuple[GoldArticle, ...]
    reference_answer: str | None
    label_status: LabelStatus
    origin: Literal["machine_candidate", "human"]
    reviewer: str | None = None
    reviewed_at: UtcDatetime | None = None
    notes: str = ""

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """Answerable questions need a grade-2 span; no-answer has no gold."""
        has_answer = any(g.grade == 2 and g.spans for g in self.gold)  # noqa: PLR2004
        if self.answerable and not has_answer:
            msg = "answerable question needs a grade-2 article with a span"
            raise ValueError(msg)
        if not self.answerable and self.gold:
            msg = "no-answer question must not carry gold"
            raise ValueError(msg)
        if self.label_status in REVIEWED and not self.reviewer:
            msg = "reviewed label needs a reviewer"
            raise ValueError(msg)
        return self


class QuestionSet(ContractModel):
    """A labelled question set bound to one corpus snapshot."""

    schema_version: Literal["search-questions.v1"]
    corpus_export_id: NonEmptyText
    corpus_digest: PrefixedSha256
    chunker_version: NonEmptyText
    split_seed: NonEmptyText
    dev_fraction: float = Field(gt=0, lt=1)
    questions: tuple[SearchQuestion, ...]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        """Question IDs are unique."""
        ids = [q.qid for q in self.questions]
        if len(ids) != len(set(ids)):
            msg = "duplicate qid"
            raise ValueError(msg)
        return self
