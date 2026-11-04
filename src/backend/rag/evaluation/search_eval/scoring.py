"""Retrieval metrics over ranked passages (pure; no I/O).

Units: an *article* ranking is the order in which articles first appear in
the ranked chunk list; a *passage* is a gold quote span, covered exactly when
one retrieved chunk contains the whole span and partially when it overlaps.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.rag.evaluation.metrics import Json, ratio
from backend.rag.evaluation.search_eval.dataset import EvalQuestion, PassageLabel

K_VALUES = (1, 3, 5, 10)
NDCG_K = 10
CITATION_K = 5
GOLD_GRADE = 2
NO_ANSWER_DEFINITION = (
    "no-answer/false-premise questions with zero candidates; retrieval alone "
    "cannot abstain (see the synthesis benchmark for false-answer rate)"
)
DIGITS = 6


@dataclass(frozen=True, slots=True)
class RankedPassage:
    """One retrieved chunk with its branch ranks (rank 1 = best)."""

    chunk_id: UUID
    article_id: UUID
    content_hash: str
    chunk_version: str
    char_start: int
    char_end: int
    text: str
    fused_rank: int
    lexical_rank: int | None
    semantic_rank: int | None
    trap: bool = False


def article_order(ranked: Sequence[RankedPassage]) -> list[UUID]:
    """Articles in order of first appearance."""
    seen: list[UUID] = []
    for item in ranked:
        if item.article_id not in seen:
            seen.append(item.article_id)
    return seen


def _covers(item: RankedPassage, label: PassageLabel) -> bool:
    return (
        item.article_id == label.article_id
        and item.content_hash == label.content_hash
        and item.char_start <= label.char_start
        and label.char_end <= item.char_end
    )


def _overlaps(item: RankedPassage, label: PassageLabel) -> bool:
    return (
        item.article_id == label.article_id
        and item.content_hash == label.content_hash
        and item.char_start < label.char_end
        and label.char_start < item.char_end
    )


@dataclass(frozen=True, slots=True)
class QuestionScore:
    """Per-question numbers that aggregate into the report."""

    question_id: str
    recall: dict[int, float | None]
    reciprocal_rank: float | None
    ndcg: float | None
    article_recall: float | None
    passage_hits: int
    passage_total: int
    exact_support: int
    overlap_support: int
    support_total: int
    fresh_top1: bool | None
    empty: bool
    traps: int
    top_chunks: tuple[str, ...]
    top_articles: tuple[str, ...] = ()
    top_spans: tuple[tuple[str, int, int], ...] = ()


def score_question(
    question: EvalQuestion, ranked: Sequence[RankedPassage], trap_hits: int = 0
) -> QuestionScore:
    """Score one ranked list against the question's labels.

    ``trap_hits`` adds trap hits a system counted itself (raw index hits that
    cannot be told apart after chunk identity is recomputed).
    """
    articles = article_order([r for r in ranked if not r.trap])
    required = set(question.required_article_ids)
    graded = {a: g for a, g in question.article_grades.items() if g > 0}
    answerable = question.answerable and bool(required)
    recall: dict[int, float | None] = {
        k: (len(required & set(articles[:k])) / len(required) if answerable else None)
        for k in K_VALUES
    }
    reciprocal = None
    if answerable:
        reciprocal = next(
            (1.0 / position for position, a in enumerate(articles, 1) if a in graded),
            0.0,
        )
    ndcg = None
    if answerable:
        gains = [
            (2 ** graded.get(a, 0) - 1) / math.log2(position + 1)
            for position, a in enumerate(articles[:NDCG_K], 1)
        ]
        ideal = sorted(graded.values(), reverse=True)[:NDCG_K]
        best = sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(ideal, 1))
        ndcg = sum(gains) / best if best else None
    article_recall = (
        len(set(graded) & set(articles[:NDCG_K])) / len(graded)
        if answerable and graded
        else None
    )
    gold = [p for p in question.passages if p.grade == GOLD_GRADE] if answerable else []
    top10 = [r for r in ranked if not r.trap][:NDCG_K]
    top5 = [r for r in ranked if not r.trap][:CITATION_K]
    fresh = None
    if answerable and question.temporal_requirement == "latest":
        fresh_ids = {p.article_id for p in question.passages if p.temporal_correct}
        fresh = bool(articles) and articles[0] in fresh_ids
    return QuestionScore(
        question_id=question.question_id,
        recall=recall,
        reciprocal_rank=reciprocal,
        ndcg=ndcg,
        article_recall=article_recall,
        passage_hits=sum(any(_covers(r, p) for r in top10) for p in gold),
        passage_total=len(gold),
        exact_support=sum(any(_covers(r, p) for r in top5) for p in gold),
        overlap_support=sum(any(_overlaps(r, p) for r in top5) for p in gold),
        support_total=len(gold),
        fresh_top1=fresh,
        empty=not [r for r in ranked if not r.trap],
        traps=sum(1 for r in ranked if r.trap) + trap_hits,
        top_chunks=tuple(str(r.chunk_id) for r in top10),
        top_articles=tuple(str(a) for a in articles[:NDCG_K]),
        top_spans=tuple((str(r.article_id), r.char_start, r.char_end) for r in top10),
    )


def _mean(values: Sequence[float | None], definition: str) -> dict[str, Json]:
    sample = [v for v in values if v is not None]
    return {
        "value": round(sum(sample) / len(sample), DIGITS) if sample else None,
        "samples": len(sample),
        "definition": definition,
    }


def aggregate(
    questions: Sequence[EvalQuestion], scores: Sequence[QuestionScore]
) -> dict[str, Json]:
    """Mean metrics plus the no-answer, freshness and scope numbers."""
    by_id = {q.question_id: q for q in questions}
    unanswerable = [s for s in scores if not by_id[s.question_id].answerable]
    metrics: dict[str, Json] = {
        f"recall@{k}": _mean(
            [s.recall[k] for s in scores],
            f"required (grade 2) articles in top {k} / required, answerable questions",
        )
        for k in K_VALUES
    }
    metrics["mrr"] = _mean(
        [s.reciprocal_rank for s in scores], "1 / rank of the first graded article"
    )
    metrics["ndcg@10"] = _mean(
        [s.ndcg for s in scores], "graded (0/1/2) nDCG over the article ranking"
    )
    metrics["article_recall@10"] = _mean(
        [s.article_recall for s in scores], "graded (>=1) articles in top 10 / graded"
    )
    metrics["passage_recall@10"] = ratio(
        sum(s.passage_hits for s in scores),
        sum(s.passage_total for s in scores),
        "grade-2 gold spans fully inside one top-10 chunk / grade-2 spans",
    )
    metrics["citation_exact_support@5"] = ratio(
        sum(s.exact_support for s in scores),
        sum(s.support_total for s in scores),
        "grade-2 gold spans fully inside one top-5 chunk (citable as-is)",
    )
    metrics["citation_overlap_support@5"] = ratio(
        sum(s.overlap_support for s in scores),
        sum(s.support_total for s in scores),
        "grade-2 gold spans overlapping a top-5 chunk",
    )
    fresh = [s.fresh_top1 for s in scores if s.fresh_top1 is not None]
    metrics["freshness@1"] = ratio(
        sum(fresh), len(fresh), "update questions whose top article is the latest"
    )
    metrics["no_answer_empty_rate"] = ratio(
        sum(s.empty for s in unanswerable),
        len(unanswerable),
        NO_ANSWER_DEFINITION,
    )
    metrics["scope_violations"] = sum(s.traps for s in scores)
    return metrics
