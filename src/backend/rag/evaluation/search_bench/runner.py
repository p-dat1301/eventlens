"""Run systems over a question set and aggregate text-free results.

Every system sees the same questions, scopes and corpus snapshot. Each
question is timed individually after a fixed warm-up; a failing question is
recorded with its error code and excluded from quality denominators (the
exclusion count is reported). Runs repeat ``repeats`` times; rankings must be
identical across repeats or the system is flagged non-deterministic.
"""

import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.use_cases.persist import AccessDeniedError
from backend.rag.domain.access import CallerScope
from backend.rag.domain.search_index import ScoredChunk
from backend.rag.evaluation.search_bench.labels import (
    REVIEWED,
    QuestionSet,
    SearchQuestion,
)
from backend.rag.evaluation.search_bench.metrics import (
    CUTOFFS,
    QuestionScores,
    article_ranking,
    auroc,
    paired_bootstrap,
    percentile,
    score_question,
)
from backend.rag.evaluation.search_bench.systems import RetrievalSystem

WARMUP_QUERIES = 3
TOP_RECORDED = 10


def scope_of(question: SearchQuestion) -> RetrievalScope:
    """Question filters as the shared retrieval scope."""
    return RetrievalScope(
        source_domains=question.filters.source_domains,
        published_after=question.filters.published_after,
        published_before=question.filters.published_before,
        allowed_article_ids=None,
    )


@dataclass(frozen=True, slots=True)
class ArticleFacts:
    """Text-free facts used for breakdowns."""

    source_domain: str
    parse_version: str
    length: int


@dataclass(slots=True)
class QuestionRun:
    """One question under one system (all repeats)."""

    qid: str
    latencies_ms: list[float] = field(default_factory=list)
    error: str | None = None
    hits: tuple[ScoredChunk, ...] = ()
    deterministic: bool = True


@dataclass(slots=True)
class SystemRun:
    """All questions under one system."""

    name: str
    description: dict[str, object]
    questions: dict[str, QuestionRun] = field(default_factory=dict)


def _key(hits: Sequence[ScoredChunk]) -> tuple[tuple[UUID, int], ...]:
    return tuple((hit.chunk.article_id, hit.chunk.chunk_index) for hit in hits)


def run_system(
    system: RetrievalSystem,
    questions: Sequence[SearchQuestion],
    repeats: int,
    clock: Callable[[], float] = time.perf_counter,
) -> SystemRun:
    """Warm up, then time each question ``repeats`` times."""
    run = SystemRun(name=system.name, description=system.describe())
    for question in questions[:WARMUP_QUERIES]:
        try:
            _ = system.search(question.question, scope_of(question))
        except Exception:  # noqa: BLE001, S112 - warm-up failures are re-measured
            continue
    for _ in range(repeats):
        for question in questions:
            item = run.questions.setdefault(question.qid, QuestionRun(question.qid))
            if item.error is not None:
                continue
            started = clock()
            try:
                hits = system.search(question.question, scope_of(question))
            except Exception as error:  # noqa: BLE001 - recorded, never hidden
                item.error = str(getattr(error, "code", type(error).__name__))
                continue
            item.latencies_ms.append((clock() - started) * 1000.0)
            if item.hits and _key(item.hits) != _key(hits):
                item.deterministic = False
            item.hits = hits
    return run


def run_benchmark(  # noqa: PLR0913 - explicit inputs keep the run reproducible
    systems: Iterable[RetrievalSystem],
    question_set: QuestionSet,
    access: AccessPolicy,
    caller: CallerScope,
    *,
    repeats: int,
    clock: Callable[[], float] = time.perf_counter,
) -> list[SystemRun]:
    """Authorize once (fixture policy), then run every system."""
    if not access.allows(caller):
        raise AccessDeniedError(caller.principal_id)
    questions = question_set.questions
    return [run_system(system, questions, repeats, clock) for system in systems]


def _primary(question: SearchQuestion) -> UUID | None:
    for gold in question.gold:
        if gold.grade == 2:  # noqa: PLR2004
            return gold.article_id
    return None


def _length_bucket(length: int) -> str:
    if length < 2000:  # noqa: PLR2004
        return "short(<2000cp)"
    if length < 5000:  # noqa: PLR2004
        return "medium(2000-4999cp)"
    return "long(>=5000cp)"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def scores_of(
    run: SystemRun, questions: Sequence[SearchQuestion]
) -> tuple[list[QuestionScores], dict[str, int]]:
    """Scores of answerable questions without errors, plus error counts."""
    scored: list[QuestionScores] = []
    errors: dict[str, int] = defaultdict(int)
    for question in questions:
        if not question.answerable:
            continue
        item = run.questions[question.qid]
        if item.error is not None:
            errors[item.error] += 1
            continue
        scored.append(score_question(question, item.hits))
    return scored, dict(errors)


def headline(
    run: SystemRun, questions: Sequence[SearchQuestion]
) -> tuple[float, float, float]:
    """Mean nDCG@10, Recall@10 and span hit@10 (0 when nothing is scored)."""
    scored, _ = scores_of(run, questions)
    if not scored:
        return 0.0, 0.0, 0.0
    n = len(scored)
    return (
        sum(s.ndcg10 for s in scored) / n,
        sum(s.recall[10] for s in scored) / n,
        sum(1.0 for s in scored if s.span_hit[10]) / n,
    )


def summarize(run: SystemRun, questions: Sequence[SearchQuestion]) -> dict[str, object]:
    """Quality and latency for one system over ``questions``."""
    scored, errors = scores_of(run, questions)
    answerable = [q for q in questions if q.answerable]
    unanswerable = [q for q in questions if not q.answerable]
    latencies = [
        value
        for question in questions
        for value in run.questions[question.qid].latencies_ms
    ]

    def top1(group: Sequence[SearchQuestion]) -> list[float]:
        return [
            run.questions[q.qid].hits[0].score if run.questions[q.qid].hits else 0.0
            for q in group
            if run.questions[q.qid].error is None
        ]

    n = len(scored)
    return {
        "questions": len(questions),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "scored": n,
        "excluded_errors": errors,
        "recall": {str(k): _mean([s.recall[k] for s in scored]) for k in (5, 10)},
        "mrr": _mean([s.mrr for s in scored]),
        "ndcg10": _mean([s.ndcg10 for s in scored]),
        "span_hit": {
            str(k): {
                "hits": sum(1 for s in scored if s.span_hit[k]),
                "of": n,
                "rate": _mean([float(s.span_hit[k]) for s in scored]),
            }
            for k in CUTOFFS
        },
        "no_answer_top1_auroc": auroc(top1(answerable), top1(unanswerable)),
        "no_answer_top1_scores": sorted(top1(unanswerable), reverse=True),
        "latency_ms": {
            "samples": len(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
        "nondeterministic_questions": sorted(
            q.qid for q in questions if not run.questions[q.qid].deterministic
        ),
    }


def breakdown(
    run: SystemRun,
    questions: Sequence[SearchQuestion],
    facts: Mapping[UUID, ArticleFacts],
) -> dict[str, dict[str, dict[str, object]]]:
    """Recall@10 / nDCG@10 / span hit@10 by category, source, parser, length."""
    groups: dict[str, dict[str, list[SearchQuestion]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for question in questions:
        groups["category"][question.category].append(question)
        primary = _primary(question)
        if primary is None or primary not in facts:
            continue
        fact = facts[primary]
        groups["source_domain"][fact.source_domain].append(question)
        groups["parse_version"][fact.parse_version].append(question)
        groups["length"][_length_bucket(fact.length)].append(question)
    result: dict[str, dict[str, dict[str, object]]] = {}
    for dimension, members in groups.items():
        result[dimension] = {}
        for value, group in sorted(members.items()):
            scored, _ = scores_of(run, group)
            ndcg, recall, span = headline(run, group)
            result[dimension][value] = {
                "answerable": sum(1 for q in group if q.answerable),
                "scored": len(scored),
                "recall10": recall if scored else None,
                "ndcg10": ndcg if scored else None,
                "span_hit10": span if scored else None,
            }
    return result


def per_question(
    run: SystemRun, questions: Sequence[SearchQuestion]
) -> list[dict[str, object]]:
    """Per-question rows for error analysis (IDs and numbers only)."""
    rows: list[dict[str, object]] = []
    for question in questions:
        item = run.questions[question.qid]
        row: dict[str, object] = {
            "qid": question.qid,
            "split": question.split,
            "category": question.category,
            "label_status": question.label_status,
            "answerable": question.answerable,
            "error": item.error,
            "latency_ms_p50": percentile(item.latencies_ms, 50),
            "top1_score": item.hits[0].score if item.hits else None,
            "top_articles": [str(a) for a in article_ranking(item.hits)[:TOP_RECORDED]],
            "top_chunks": [
                f"{hit.chunk.article_id}#{hit.chunk.chunk_index}"
                for hit in item.hits[:TOP_RECORDED]
            ],
        }
        if question.answerable and item.error is None:
            scores = score_question(question, item.hits)
            row.update(
                recall5=scores.recall[5],
                recall10=scores.recall[10],
                rr=scores.mrr,
                ndcg10=scores.ndcg10,
                span_hit5=scores.span_hit[5],
                span_hit10=scores.span_hit[10],
            )
        rows.append(row)
    return rows


def _ndcg(scores: QuestionScores) -> float:
    return scores.ndcg10


def _recall10(scores: QuestionScores) -> float:
    return scores.recall[10]


def _mrr(scores: QuestionScores) -> float:
    return scores.mrr


def compare(
    left: SystemRun,
    right: SystemRun,
    questions: Sequence[SearchQuestion],
    seed: int,
) -> dict[str, object]:
    """Paired bootstrap of nDCG@10, Recall@10 and MRR (answerable, no errors)."""
    usable = [
        q
        for q in questions
        if q.answerable
        and left.questions[q.qid].error is None
        and right.questions[q.qid].error is None
    ]
    a = [score_question(q, left.questions[q.qid].hits) for q in usable]
    b = [score_question(q, right.questions[q.qid].hits) for q in usable]
    out: dict[str, object] = {"left": left.name, "right": right.name, "n": len(usable)}
    metrics: tuple[tuple[str, Callable[[QuestionScores], float]], ...] = (
        ("ndcg10", _ndcg),
        ("recall10", _recall10),
        ("mrr", _mrr),
    )
    for metric, pick in metrics:
        delta = paired_bootstrap([pick(s) for s in a], [pick(s) for s in b], seed=seed)
        out[metric] = {
            "mean_delta": delta.mean,
            "ci95": [delta.low, delta.high],
            "wins": delta.wins,
            "losses": delta.losses,
            "ties": delta.ties,
            "clear": delta.low > 0 or delta.high < 0,
        }
    return out


def subsets(question_set: QuestionSet) -> dict[str, list[SearchQuestion]]:
    """Named question subsets reported separately."""
    usable = [
        q for q in question_set.questions if q.label_status != "reviewed_rejected"
    ]
    reviewed = [q for q in usable if q.label_status in REVIEWED]
    return {
        "dev": [q for q in usable if q.split == "dev"],
        "test": [q for q in usable if q.split == "test"],
        "all": usable,
        "reviewed_test": [q for q in reviewed if q.split == "test"],
    }
