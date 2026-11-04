"""Grouped metrics, gold-span ceilings and text-free per-question rows (pure).

Groups (a question may belong to several, e.g. one per required source):

* ``split``: ``dev`` / ``test`` (labels file); ``all`` for the fixture;
* ``category``: label category (proper noun, datetime, paraphrase, filter,
  multi-source, near-duplicate, no-answer);
* ``numeric_query``: *derived* — the query contains a digit or a quantity
  word (heuristic, not a label);
* ``source``: source domain of each required article;
* ``article_length``: corpus tercile (code points) of the required articles;
* ``parser``: ``parse_version/normalization_version`` of required articles.
"""

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from backend.rag.evaluation.metrics import Json, ratio
from backend.rag.evaluation.search_eval.dataset import (
    EvalQuestion,
    PassageLabel,
    SearchDataset,
)
from backend.rag.evaluation.search_eval.scoring import (
    GOLD_GRADE,
    QuestionScore,
    aggregate,
)
from backend.rag.evaluation.search_eval.systems import ChunkSpan

if TYPE_CHECKING:
    from uuid import UUID

NUMERIC_QUERY: Final = re.compile(
    r"\d|bao nhiêu|mấy |tỷ lệ|phần trăm|bao lâu", re.IGNORECASE
)
NUMERIC_DEFINITION: Final = (
    "derived heuristic: query has a digit or bao nhiêu/mấy/tỷ lệ/phần trăm/bao lâu"
)
SPLITS: Final = ("dev", "test")


def length_thresholds(dataset: SearchDataset) -> tuple[int, int]:
    """Corpus terciles of article length (code points)."""
    lengths = sorted(len(a.normalized_content) for a in dataset.articles)
    if not lengths:
        return (0, 0)
    return (lengths[len(lengths) // 3], lengths[(2 * len(lengths)) // 3])


def question_groups(
    question: EvalQuestion, dataset: SearchDataset, thresholds: tuple[int, int]
) -> dict[str, tuple[str, ...]]:
    """Group memberships of one question (text-free)."""
    groups: dict[str, tuple[str, ...]] = {
        "split": (question.split,),
        "numeric_query": (
            "numeric" if NUMERIC_QUERY.search(question.query) else "non_numeric",
        ),
    }
    if question.category is not None:
        groups["category"] = (question.category,)
    required = [a for a in question.required_article_ids if a in dataset.facts]
    if required:
        facts = [dataset.facts[a] for a in required]
        groups["source"] = tuple(sorted({f.source_domain for f in facts}))
        short, long = thresholds
        groups["article_length"] = tuple(
            sorted(
                {
                    "short" if f.chars < short else "long" if f.chars >= long else "mid"
                    for f in facts
                }
            )
        )
        groups["parser"] = tuple(
            sorted({f"{f.parse_version}/{f.normalization_version}" for f in facts})
        )
    return groups


def breakdown(
    dataset: SearchDataset, scores: Sequence[QuestionScore]
) -> dict[str, Json]:
    """Aggregate metrics per (split, group kind, group value)."""
    thresholds = length_thresholds(dataset)
    by_id = {q.question_id: q for q in dataset.questions}
    members: dict[tuple[str, str, str], list[QuestionScore]] = {}
    for score in scores:
        question = by_id[score.question_id]
        for kind, values in question_groups(question, dataset, thresholds).items():
            if kind == "split":
                continue
            for value in values:
                for split in ("all", question.split):
                    members.setdefault((split, kind, value), []).append(score)
    out: dict[str, Json] = {}
    for (split, kind, value), group in sorted(members.items()):
        split_entry = out.setdefault(split, {})
        assert isinstance(split_entry, dict)  # noqa: S101
        kind_entry = split_entry.setdefault(kind, {})
        assert isinstance(kind_entry, dict)  # noqa: S101
        metrics = aggregate(dataset.questions, group)
        kind_entry[value] = {
            "questions": len(group),
            "recall@10": metrics["recall@10"],
            "mrr": metrics["mrr"],
            "ndcg@10": metrics["ndcg@10"],
            "passage_recall@10": metrics["passage_recall@10"],
        }
    out["definitions"] = {
        "numeric_query": NUMERIC_DEFINITION,
        "article_length": (
            f"corpus terciles: short < {thresholds[0]} <= mid < "
            f"{thresholds[1]} <= long (code points)"
        ),
    }
    return out


def by_split(
    dataset: SearchDataset, scores: Sequence[QuestionScore]
) -> dict[str, Json]:
    """Full metric set per split (selection on dev, reporting on test)."""
    by_id = {q.question_id: q for q in dataset.questions}
    out: dict[str, Json] = {}
    for split in SPLITS:
        subset = [s for s in scores if by_id[s.question_id].split == split]
        if subset:
            out[split] = aggregate(dataset.questions, subset)
    return out


def _gold(dataset: SearchDataset) -> list[PassageLabel]:
    return [
        p
        for q in dataset.questions
        if q.answerable
        for p in q.passages
        if p.grade == GOLD_GRADE
    ]


def span_ceiling(
    dataset: SearchDataset, layout: Sequence[ChunkSpan]
) -> dict[str, Json]:
    """Gold spans a chunk set can cite at all, independent of ranking.

    Recomputed from offsets for every chunker (never from old chunk IDs):
    ``contained`` = some chunk holds the whole span, ``overlapped`` = some chunk
    overlaps it, ``split_across`` = overlapped but never contained.
    """
    chunks: dict[tuple[UUID, str], list[tuple[int, int]]] = {}
    for article_id, content_hash, start, end in layout:
        chunks.setdefault((article_id, content_hash), []).append((start, end))
    gold = _gold(dataset)
    contained = overlapped = 0
    for span in gold:
        windows = chunks.get((span.article_id, span.content_hash), [])
        inside = any(s <= span.char_start and span.char_end <= e for s, e in windows)
        touch = any(s < span.char_end and span.char_start < e for s, e in windows)
        contained += inside
        overlapped += touch
    return {
        "contained": ratio(contained, len(gold), "grade-2 spans inside one chunk"),
        "overlapped": ratio(overlapped, len(gold), "grade-2 spans touching a chunk"),
        "split_across": overlapped - contained,
    }


def question_rows(scores: Sequence[QuestionScore]) -> list[Json]:
    """Text-free per-question numbers for paired comparison and error analysis."""
    return [
        {
            "question_id": s.question_id,
            "recall@1": s.recall[1],
            "recall@5": s.recall[5],
            "recall@10": s.recall[10],
            "rr": s.reciprocal_rank,
            "ndcg@10": s.ndcg,
            "passage_hits@10": s.passage_hits,
            "passage_total": s.passage_total,
            "empty": s.empty,
            "top_articles": list(s.top_articles),
            "top_spans": [list(span) for span in s.top_spans],
        }
        for s in scores
    ]
