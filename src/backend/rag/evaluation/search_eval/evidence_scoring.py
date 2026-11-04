"""Metrics for the selection (B) and payload (C) stages (pure; text-free).

* required coverage: grade-2 articles present / grade-2 articles;
* all required: every grade-2 article present (multi-source completeness);
* graded coverage: grade >= 1 articles present / graded articles;
* gold passage: a grade-2 gold span lies fully inside one sent span (exact)
  or overlaps one (overlap); spans are compared on the RAG NFC offsets of the
  same article and content hash;
* distribution: distinct articles and spans per article.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from backend.rag.evaluation.metrics import Json, ratio
from backend.rag.evaluation.search_eval.dataset import EvalQuestion
from backend.rag.evaluation.search_eval.scoring import GOLD_GRADE
from backend.rag.evaluation.search_eval.stages import EvidenceStages, StageSpan

DIGITS = 6
STAGES = ("selected", "payload")


@dataclass(frozen=True, slots=True)
class StageScore:
    """One stage of one question."""

    required_coverage: float | None
    all_required: bool | None
    graded_coverage: float | None
    gold_exact: int
    gold_overlap: int
    gold_total: int
    articles: int
    spans: int
    spans_per_article: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EvidenceScore:
    """B and C for one question plus counters from the run."""

    question_id: str
    category: str | None
    selected: StageScore
    payload: StageScore
    stages: EvidenceStages


def _stage(question: EvalQuestion, spans: Sequence[StageSpan]) -> StageScore:
    articles = {span.article_id for span in spans}
    required = {str(a) for a in question.required_article_ids}
    graded = {str(a) for a, g in question.article_grades.items() if g > 0}
    answerable = question.answerable and bool(required)
    gold = [p for p in question.passages if p.grade == GOLD_GRADE] if answerable else []

    def inside(label_article: str, content_hash: str, start: int, end: int) -> bool:
        return any(
            s.article_id == label_article
            and s.content_hash == content_hash
            and s.start <= start
            and end <= s.end
            for s in spans
        )

    def overlaps(label_article: str, content_hash: str, start: int, end: int) -> bool:
        return any(
            s.article_id == label_article
            and s.content_hash == content_hash
            and s.start < end
            and start < s.end
            for s in spans
        )

    per_article = Counter(span.article_id for span in spans)
    return StageScore(
        required_coverage=len(required & articles) / len(required)
        if answerable
        else None,
        all_required=required <= articles if answerable else None,
        graded_coverage=len(graded & articles) / len(graded)
        if answerable and graded
        else None,
        gold_exact=sum(
            inside(str(p.article_id), p.content_hash, p.char_start, p.char_end)
            for p in gold
        ),
        gold_overlap=sum(
            overlaps(str(p.article_id), p.content_hash, p.char_start, p.char_end)
            for p in gold
        ),
        gold_total=len(gold),
        articles=len(articles),
        spans=len(spans),
        spans_per_article=tuple(sorted(per_article.values(), reverse=True)),
    )


def score_evidence(question: EvalQuestion, stages: EvidenceStages) -> EvidenceScore:
    """Score B (selection) and C (payload) of one answer."""
    return EvidenceScore(
        question_id=question.question_id,
        category=question.category,
        selected=_stage(question, stages.selected),
        payload=_stage(question, stages.payload),
        stages=stages,
    )


def _mean(values: Sequence[float | None], definition: str) -> dict[str, Json]:
    sample = [v for v in values if v is not None]
    return {
        "value": round(sum(sample) / len(sample), DIGITS) if sample else None,
        "samples": len(sample),
        "definition": definition,
    }


def _stage_metrics(scores: Sequence[StageScore]) -> dict[str, Json]:
    answerable = [s for s in scores if s.all_required is not None]
    histogram = Counter(n for s in scores for n in s.spans_per_article)
    return {
        "required_article_coverage": _mean(
            [s.required_coverage for s in scores],
            "grade-2 articles present / grade-2 articles (answerable questions)",
        ),
        "all_required_articles": ratio(
            sum(1 for s in answerable if s.all_required),
            len(answerable),
            "answerable questions with every grade-2 article present",
        ),
        "graded_article_coverage": _mean(
            [s.graded_coverage for s in scores],
            "grade>=1 articles present / graded articles",
        ),
        "gold_passage_exact": ratio(
            sum(s.gold_exact for s in scores),
            sum(s.gold_total for s in scores),
            "grade-2 gold spans fully inside one span",
        ),
        "gold_passage_overlap": ratio(
            sum(s.gold_overlap for s in scores),
            sum(s.gold_total for s in scores),
            "grade-2 gold spans overlapping a span",
        ),
        "distinct_articles_mean": round(
            sum(s.articles for s in scores) / len(scores), DIGITS
        )
        if scores
        else None,
        "spans_mean": round(sum(s.spans for s in scores) / len(scores), DIGITS)
        if scores
        else None,
        "spans_per_article_histogram": {
            str(k): histogram[k] for k in sorted(histogram)
        },
    }


def aggregate_evidence(scores: Sequence[EvidenceScore]) -> dict[str, Json]:
    """Deterministic B/C metrics plus selection/packing counters."""
    stages = [s.stages for s in scores]
    selected_spans = sum(s.selected.spans for s in scores)
    payload_spans = sum(s.payload.spans for s in scores)
    return {
        "questions": len(scores),
        "selected": _stage_metrics([s.selected for s in scores]),
        "payload": _stage_metrics([s.payload for s in scores]),
        "overlap_dropped": sum(s.overlap_dropped for s in stages),
        "cap_dropped": sum(s.cap_dropped for s in stages),
        "branch_cap_dropped": sum(s.branch_cap_dropped for s in stages),
        "backfill_calls": sum(s.backfill_calls for s in stages),
        "dropped_at_final_check": sum(s.dropped_at_final_check for s in stages),
        "selected_spans_not_in_payload": selected_spans - payload_spans,
        "truncated_spans": sum(s.truncated_spans for s in stages),
        "omitted_articles": sum(s.omitted_articles for s in stages),
        "payload_tokens_mean": round(
            sum(s.payload_tokens for s in stages) / len(stages), 1
        )
        if stages
        else None,
        "payload_tokens_max": max((s.payload_tokens for s in stages), default=None),
        "token_budget": max((s.token_budget for s in stages), default=None),
        "empty_payloads": sum(1 for s in stages if s.empty_reason is not None),
        "freshness_decisions": dict(
            sorted(Counter(s.freshness for s in stages).items())
        ),
        "freshness_boosted": sum(s.freshness_boosted for s in stages),
        "freshness_max_promotion": max(
            (s.freshness_max_promotion for s in stages), default=0
        ),
        "fill_spans_sent": sum(s.fill_spans_sent for s in stages),
    }


def call_counts(scores: Sequence[EvidenceScore]) -> dict[str, Json]:
    """Storage/embedding calls (deterministic per configuration)."""
    stages = [s.stages for s in scores]
    return {
        "storage_calls": sum(s.storage_calls for s in stages),
        "embedding_calls": sum(s.embedding_calls for s in stages),
        "storage_calls_per_query": round(
            sum(s.storage_calls for s in stages) / len(stages), 3
        )
        if stages
        else None,
    }


def evidence_rows(scores: Sequence[EvidenceScore]) -> dict[str, dict[str, Json]]:
    """Per-question B/C fields merged into ``per_question`` rows."""
    out: dict[str, dict[str, Json]] = {}
    for s in scores:
        row: dict[str, Json] = {}
        for name, stage in (("b", s.selected), ("c", s.payload)):
            row[f"{name}_required_cov"] = stage.required_coverage
            row[f"{name}_all_required"] = (
                None if stage.all_required is None else float(stage.all_required)
            )
            row[f"{name}_gold_exact"] = stage.gold_exact
            row[f"{name}_gold_frac"] = (
                stage.gold_exact / stage.gold_total if stage.gold_total else None
            )
            row[f"{name}_articles"] = stage.articles
            row[f"{name}_spans"] = stage.spans
        row["c_tokens"] = s.stages.payload_tokens
        row["c_payload"] = [
            [span.article_id, span.start, span.end] for span in s.stages.payload
        ]
        out[s.question_id] = row
    return out
