"""Versioned eval inputs from ``fixtures/rag``: corpus, gold labels, questions.

Both eval targets read this one loader, so they score the same corpus and the
same question set; ``fixture_digest`` pins the exact bytes in every report.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from backend.shared.contracts import ArticleEnrichment

from .metrics import Json

FIXTURES = Path(__file__).parents[4] / "fixtures/rag"
# Fixed query/enrichment clock: after every fixture publication time.
EVAL_CLOCK = datetime(2026, 4, 1, tzinfo=UTC)
_OBJECT = TypeAdapter(dict[str, object])
_ITEMS = TypeAdapter(list[dict[str, object]])
_STRINGS = TypeAdapter(list[str])
_REFERENCES = TypeAdapter(list[dict[str, str]])
_VERSION_KEYS = (
    "corpus_version",
    "corpus_count",
    "normalization_version",
    "enrichment_version",
    "model_version",
    "prompt_version",
    "embedding_version",
    "provenance",
    "license",
)


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    """One fixture question with its gold references and scope."""

    question_id: str
    text: str
    answer_mode: str
    time_basis: str
    expected_articles: tuple[UUID, ...]
    allowed_articles: tuple[UUID, ...]
    # Gold spans as (article_id, start, end) code-point ranges of the fixture
    # evidence spans named in expected_references.
    expected_spans: tuple[tuple[UUID, int, int], ...]


@dataclass(frozen=True, slots=True)
class SemanticCase:
    """Hand-labelled claim text against one article's cited span."""

    case_id: str
    article_id: UUID
    claim_text: str
    label: str


@dataclass(frozen=True, slots=True)
class EvalCorpus:
    """Everything both eval targets score against."""

    root: Path
    articles: tuple[ArticleDocument, ...]
    gold: dict[UUID, ArticleEnrichment]
    questions: tuple[EvalQuestion, ...]
    semantic_cases: tuple[SemanticCase, ...]
    coverage: dict[str, tuple[str, ...]]
    # Ranking comparison scope: every permitted article (distractors
    # included); restricted articles excluded (ranking_scope.v1.json).
    ranking_scope: frozenset[UUID]
    restricted: frozenset[UUID]

    def article(self, article_id: UUID) -> ArticleDocument:
        """Fixture article by ID."""
        return next(a for a in self.articles if a.article_id == article_id)


def _read(path: Path) -> dict[str, object]:
    return _OBJECT.validate_json(path.read_bytes())


def _gold_spans(
    references: list[dict[str, str]], articles: tuple[ArticleDocument, ...]
) -> tuple[tuple[UUID, int, int], ...]:
    spans = {
        span.span_id: (span.article_id, span.start_offset, span.end_offset)
        for article in articles
        for span in article.evidence_spans
    }
    return tuple(spans[UUID(r["span_id"])] for r in references)


def _questions(
    root: Path, articles: tuple[ArticleDocument, ...]
) -> tuple[EvalQuestion, ...]:
    items = _ITEMS.validate_python(_read(root / "questions.v1.json")["questions"])
    return tuple(
        EvalQuestion(
            question_id=str(item["question_id"]),
            text=str(item["question"]),
            answer_mode=str(item["answer_mode"]),
            time_basis=str(item["time_basis"]),
            expected_articles=tuple(
                dict.fromkeys(
                    UUID(r["article_id"])
                    for r in _REFERENCES.validate_python(item["expected_references"])
                )
            ),
            allowed_articles=tuple(
                UUID(value)
                for value in _STRINGS.validate_python(item["allowed_article_ids"])
            ),
            expected_spans=_gold_spans(
                _REFERENCES.validate_python(item["expected_references"]), articles
            ),
        )
        for item in items
    )


def _semantic(root: Path) -> tuple[SemanticCase, ...]:
    items = _ITEMS.validate_python(_read(root / "semantic_support.v1.json")["cases"])
    return tuple(
        SemanticCase(
            case_id=str(item["case_id"]),
            article_id=UUID(str(item["article_id"])),
            claim_text=str(item["claim_text"]),
            label=str(item["label"]),
        )
        for item in items
    )


def _coverage(root: Path) -> dict[str, tuple[str, ...]]:
    cases = _ITEMS.validate_python(_read(root / "coverage.v1.json")["cases"])
    return {
        str(case["purpose"]): tuple(_STRINGS.validate_python(case["question_ids"]))
        for case in cases
    }


class RankingScopeError(ValueError):
    """Ranking scope overlaps restricted articles or misses gold articles."""


def _ranking_scope(
    root: Path, questions: tuple[EvalQuestion, ...]
) -> tuple[frozenset[UUID], frozenset[UUID]]:
    raw = _read(root / "ranking_scope.v1.json")
    allowed = frozenset(
        UUID(v) for v in _STRINGS.validate_python(raw["allowed_article_ids"])
    )
    restricted = frozenset(
        UUID(v) for v in _STRINGS.validate_python(raw["restricted_article_ids"])
    )
    gold = {a for q in questions for a in q.expected_articles}
    if allowed & restricted or not gold <= allowed:
        raise RankingScopeError(str(root / "ranking_scope.v1.json"))
    return allowed, restricted


def load_corpus(root: Path = FIXTURES) -> EvalCorpus:
    """Validated corpus, manual gold enrichment, questions and semantic labels."""
    gold = {
        record.article_id: record
        for record in (
            ArticleEnrichment.model_validate_json(path.read_bytes())
            for path in sorted((root / "enrichments").glob("*.json"))
        )
    }
    articles = load_fixture_articles(root)
    questions = _questions(root, articles)
    ranking, restricted = _ranking_scope(root, questions)
    return EvalCorpus(
        root=root,
        articles=articles,
        gold=gold,
        questions=questions,
        semantic_cases=_semantic(root),
        coverage=_coverage(root),
        ranking_scope=ranking,
        restricted=restricted,
    )


def fixture_digest(root: Path = FIXTURES) -> str:
    """SHA-256 over every fixture file path and its bytes, in sorted order."""
    hasher = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*.json") if p.is_file()):
        hasher.update(path.relative_to(root).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
    return "sha256:" + hasher.hexdigest()


def corpus_versions(root: Path = FIXTURES) -> dict[str, Json]:
    """Manifest versions plus question/label schema versions and file digest."""
    manifest = _read(root / "manifest.v1.json")
    versions: dict[str, Json] = {
        key: value
        for key in _VERSION_KEYS
        if isinstance(value := manifest.get(key), str | int)
    }
    versions["questions_schema"] = str(
        _read(root / "questions.v1.json")["schema_version"]
    )
    versions["semantic_labels_schema"] = str(
        _read(root / "semantic_support.v1.json")["schema_version"]
    )
    ranking = _read(root / "ranking_scope.v1.json")
    versions["ranking_scope"] = f"{ranking['schema_version']}/{ranking['scope_id']}"
    versions["fixture_digest"] = fixture_digest(root)
    return versions
