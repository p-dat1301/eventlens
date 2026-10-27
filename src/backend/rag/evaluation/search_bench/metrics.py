"""Retrieval metrics over ranked chunks, with explicit denominators.

Article ranking = first occurrence order of articles in the chunk ranking.
Relevance is graded (2 answers, 1 related); recall and MRR count grade >= 1,
nDCG uses gain ``2**grade - 1``. A gold-span hit is a top-k chunk of the gold
revision whose ``[start, end)`` overlaps a grade-2 span.
"""

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.rag.domain.search_index import ScoredChunk
from backend.rag.evaluation.search_bench.labels import SearchQuestion

CUTOFFS = (1, 3, 5, 10, 20)


def article_ranking(hits: Sequence[ScoredChunk]) -> list[UUID]:
    """Unique articles in first-hit order."""
    seen: dict[UUID, None] = {}
    for hit in hits:
        seen.setdefault(hit.chunk.article_id, None)
    return list(seen)


def _grades(question: SearchQuestion) -> dict[UUID, int]:
    return {gold.article_id: gold.grade for gold in question.gold}


def recall_at(question: SearchQuestion, articles: Sequence[UUID], k: int) -> float:
    """Share of relevant (grade >= 1) articles in the top ``k``."""
    relevant = _grades(question)
    found = sum(1 for article in articles[:k] if article in relevant)
    return found / len(relevant)


def reciprocal_rank(question: SearchQuestion, articles: Sequence[UUID]) -> float:
    """1 / rank of the first relevant article; 0 when none is retrieved."""
    relevant = _grades(question)
    for index, article in enumerate(articles, start=1):
        if article in relevant:
            return 1.0 / index
    return 0.0


def ndcg_at(question: SearchQuestion, articles: Sequence[UUID], k: int) -> float:
    """Graded nDCG@k with gain ``2**grade - 1``."""
    grades = _grades(question)
    dcg = sum(
        (2 ** grades.get(article, 0) - 1) / math.log2(index + 1)
        for index, article in enumerate(articles[:k], start=1)
    )
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return dcg / idcg if idcg else 0.0


def span_hit_at(question: SearchQuestion, hits: Sequence[ScoredChunk], k: int) -> bool:
    """Some top-``k`` chunk overlaps a grade-2 gold span of its revision."""
    spans = [
        (gold.article_id, gold.content_hash, span.start, span.end)
        for gold in question.gold
        if gold.grade == 2  # noqa: PLR2004
        for span in gold.spans
    ]
    for hit in hits[:k]:
        chunk = hit.chunk
        for article_id, content_hash, start, end in spans:
            if (
                chunk.article_id == article_id
                and chunk.content_hash == content_hash
                and chunk.start_offset < end
                and start < chunk.end_offset
            ):
                return True
    return False


@dataclass(frozen=True, slots=True)
class QuestionScores:
    """Per-question metrics (answerable questions only)."""

    recall: dict[int, float]
    mrr: float
    ndcg10: float
    span_hit: dict[int, bool]


def score_question(
    question: SearchQuestion, hits: Sequence[ScoredChunk]
) -> QuestionScores:
    """All metrics for one answerable question."""
    articles = article_ranking(hits)
    return QuestionScores(
        recall={k: recall_at(question, articles, k) for k in CUTOFFS},
        mrr=reciprocal_rank(question, articles),
        ndcg10=ndcg_at(question, articles, 10),
        span_hit={k: span_hit_at(question, hits, k) for k in CUTOFFS},
    )


def auroc(positives: Sequence[float], negatives: Sequence[float]) -> float | None:
    """P(score of a positive > score of a negative), ties count half."""
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values)


@dataclass(frozen=True, slots=True)
class PairedDelta:
    """Mean of ``a - b`` with a seeded percentile-bootstrap 95% interval."""

    mean: float
    low: float
    high: float
    n: int
    wins: int
    losses: int
    ties: int


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    *,
    seed: int,
    resamples: int = 10_000,
    statistic: Callable[[Sequence[float]], float] | None = None,
) -> PairedDelta:
    """Paired bootstrap over questions; same seed -> same interval."""
    deltas = [x - y for x, y in zip(a, b, strict=True)]
    n = len(deltas)
    stat = statistic or _average
    if n == 0:
        return PairedDelta(0.0, 0.0, 0.0, 0, 0, 0, 0)
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    samples = sorted(
        stat([deltas[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)
    )
    low = samples[int(0.025 * resamples)]
    high = samples[min(resamples - 1, int(0.975 * resamples))]
    return PairedDelta(
        mean=stat(deltas),
        low=low,
        high=high,
        n=n,
        wins=sum(1 for d in deltas if d > 0),
        losses=sum(1 for d in deltas if d < 0),
        ties=sum(1 for d in deltas if d == 0),
    )
