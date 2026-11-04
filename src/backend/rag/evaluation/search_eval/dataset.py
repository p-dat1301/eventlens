"""Typed loader for ``fixtures/rag/search-selection`` (synthetic, versioned).

External handoff corpora (real articles + machine-candidate labels) load
through ``handoff_dataset.load_handoff_dataset`` into the same types.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final, cast
from uuid import UUID

from backend.rag.domain.article_document import ArticleDocument

FIXTURE: Final = Path(__file__).parents[5] / "fixtures/rag/search-selection"
ANSWERABLE: Final = "answerable"
FIXTURE_KIND: Final = "fixture"
# Frozen query clock of the synthetic fixture (freshness bonus input).
FIXTURE_AS_OF: Final = datetime(2026, 4, 30, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class QuestionFilters:
    """Per-question search filters (empty = unrestricted)."""

    source_domains: tuple[str, ...] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None

    @property
    def empty(self) -> bool:
        """No restriction at all."""
        return (
            not self.source_domains
            and self.published_after is None
            and self.published_before is None
        )


@dataclass(frozen=True, slots=True)
class PassageLabel:
    """Graded gold passage (exact code-point span of the article text)."""

    article_id: UUID
    content_hash: str
    char_start: int
    char_end: int
    grade: int
    temporal_correct: bool


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    """One labelled question with its graded article and passage judgments."""

    question_id: str
    query: str
    intent: str
    answerability: str
    required_article_ids: tuple[UUID, ...]
    required_chunk_ids: tuple[UUID, ...]
    required_claims: tuple[str, ...]
    temporal_requirement: str | None
    allowed_scope: str
    article_grades: dict[UUID, int]
    passages: tuple[PassageLabel, ...]
    split: str = "all"
    category: str | None = None
    label_status: str = "author-labeled"
    origin: str | None = None
    ai_prescreen: str | None = None
    filters: QuestionFilters = field(default_factory=QuestionFilters)
    reference_answer: str | None = None

    @property
    def answerable(self) -> bool:
        """Answerable questions carry required evidence."""
        return self.answerability == ANSWERABLE


@dataclass(frozen=True, slots=True)
class ArticleFacts:
    """Text-free per-article facts used to group metrics."""

    source_domain: str
    parse_version: str | None
    normalization_version: str | None
    upstream_content_hash: str | None
    chars: int


@dataclass(frozen=True, slots=True)
class SearchDataset:
    """Articles, questions and the digests that identify them."""

    articles: tuple[ArticleDocument, ...]
    questions: tuple[EvalQuestion, ...]
    manifest: dict[str, object]
    fixture_digest: str
    corpus_digest: str
    kind: str = FIXTURE_KIND
    as_of: datetime = FIXTURE_AS_OF
    export_id: str | None = None
    articles_sha256: str | None = None
    label_digest: str | None = None
    label_issues: tuple[dict[str, str], ...] = ()
    facts: dict[UUID, ArticleFacts] = field(default_factory=dict[UUID, "ArticleFacts"])

    @property
    def labels(self) -> str:
        """Label provenance statement from the manifest/labels file."""
        return str(self.manifest.get("labels", "unknown"))


def corpus_digest_of(articles: tuple[ArticleDocument, ...]) -> str:
    """SHA-256 over sorted ``article_id:content_hash`` lines (RAG revisions)."""
    lines = sorted(f"{a.article_id}:{a.content_hash}" for a in articles)
    return f"sha256:{sha256(chr(10).join(lines).encode()).hexdigest()}"


def _lines(path: Path) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _str(row: dict[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise TypeError(msg)
    return value


def _int(row: dict[str, object], key: str) -> int:
    value = row[key]
    if not isinstance(value, int):
        msg = f"{key} must be an integer"
        raise TypeError(msg)
    return value


def _strings(row: dict[str, object], key: str) -> tuple[str, ...]:
    value = row[key]
    if not isinstance(value, list):
        msg = f"{key} must be a list"
        raise TypeError(msg)
    return tuple(str(item) for item in cast("list[object]", value))


def load_dataset(directory: Path = FIXTURE) -> SearchDataset:
    """Load and cross-check the fixture (file digests must match the manifest)."""
    manifest = cast(
        "dict[str, object]",
        json.loads((directory / "manifest.json").read_text(encoding="utf-8")),
    )
    files = cast("dict[str, str]", manifest["files"])
    for name, expected in files.items():
        actual = "sha256:" + sha256((directory / name).read_bytes()).hexdigest()
        if actual != expected:
            msg = f"fixture file {name} does not match manifest digest"
            raise ValueError(msg)
    articles = tuple(
        ArticleDocument.model_validate_json(line)
        for line in (directory / "articles.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    )
    passages: dict[str, list[PassageLabel]] = {}
    grades: dict[str, dict[UUID, int]] = {}
    for row in _lines(directory / "qrels.jsonl"):
        qid = _str(row, "question_id")
        aid = UUID(_str(row, "article_id"))
        if row["level"] == "article":
            grades.setdefault(qid, {})[aid] = _int(row, "grade")
        else:
            passages.setdefault(qid, []).append(
                PassageLabel(
                    article_id=aid,
                    content_hash=_str(row, "content_hash"),
                    char_start=_int(row, "char_start"),
                    char_end=_int(row, "char_end"),
                    grade=_int(row, "grade"),
                    temporal_correct=bool(row["temporal_correct"]),
                )
            )
    questions = tuple(
        EvalQuestion(
            question_id=_str(row, "question_id"),
            query=_str(row, "query"),
            intent=_str(row, "intent"),
            answerability=_str(row, "answerability"),
            required_article_ids=tuple(
                UUID(v) for v in _strings(row, "required_article_ids")
            ),
            required_chunk_ids=tuple(
                UUID(v) for v in _strings(row, "required_chunk_ids")
            ),
            required_claims=_strings(row, "required_claims"),
            temporal_requirement=(
                None
                if row["temporal_requirement"] is None
                else _str(row, "temporal_requirement")
            ),
            allowed_scope=_str(row, "allowed_scope"),
            article_grades=grades.get(_str(row, "question_id"), {}),
            passages=tuple(passages.get(_str(row, "question_id"), ())),
            split=str(row.get("split", "all")),
            category=None if row.get("category") is None else str(row["category"]),
            filters=_filters(row.get("filters")),
        )
        for row in _lines(directory / "questions.jsonl")
    )
    fixture_digest = (
        "sha256:" + sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    )
    clock = manifest.get("query_clock")
    return SearchDataset(
        articles=articles,
        questions=questions,
        manifest=manifest,
        fixture_digest=fixture_digest,
        corpus_digest=str(manifest["corpus_digest"]),
        as_of=FIXTURE_AS_OF if clock is None else datetime.fromisoformat(str(clock)),
    )


def _filters(raw: object) -> QuestionFilters:
    """Optional per-question filters (absent in the search-selection fixture)."""
    if not isinstance(raw, dict):
        return QuestionFilters()
    body = cast("dict[str, object]", raw)
    domains = cast("list[object]", body.get("source_domains") or [])
    after, before = body.get("published_after"), body.get("published_before")
    return QuestionFilters(
        source_domains=tuple(str(d) for d in domains),
        published_after=None if after is None else datetime.fromisoformat(str(after)),
        published_before=None
        if before is None
        else datetime.fromisoformat(str(before)),
    )
