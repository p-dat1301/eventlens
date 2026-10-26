"""Per-mode RAG aggregates over per-question records (pure, no adapters)."""

import math

from .corpus import EvalCorpus
from .metrics import Json, ratio

K_VALUES = (1, 3, 5, 10)
NDCG_K = 10


def _int(record: dict[str, Json], key: str) -> int:
    value = record.get(key)
    return value if isinstance(value, int) else 0


def _recall(records: dict[str, dict[str, Json]], k: int) -> dict[str, Json]:
    hits = expected = 0
    for record in records.values():
        cell = record.get(f"recall_at_{k}")
        if isinstance(cell, dict):
            hits += _int(cell, "hits")
            expected += _int(cell, "expected")
    return ratio(
        hits,
        expected,
        f"expected articles in top {k} / expected (answerable questions)",
    )


def _count(record: dict[str, Json], key: str) -> int:
    value = record.get(key)
    return len(value) if isinstance(value, list) else 0


def _violations(record: dict[str, Json]) -> int:
    """Out-of-scope article IDs over all stages; malformed counts as 1."""
    stages = record.get("scope_violations")
    if not isinstance(stages, dict):
        return 1
    return sum(_count(stages, name) for name in ("retrieved", "sent_to_model", "cited"))


def _ranks(record: dict[str, Json]) -> list[int | None]:
    value = record.get("ranks_of_expected")
    if not isinstance(value, list):
        return []
    return [rank if isinstance(rank, int) else None for rank in value]


def _ranking_quality(
    records: dict[str, dict[str, Json]], answerable: list[str]
) -> dict[str, Json]:
    """MRR and nDCG@10 over answerable questions (missing ranks score 0)."""
    reciprocal: list[float] = []
    ndcg: list[float] = []
    for qid in answerable:
        ranks = _ranks(records.get(qid, {}))
        found = [r for r in ranks if r is not None]
        reciprocal.append(1 / min(found) if found else 0.0)
        dcg = sum(1 / math.log2(r + 1) for r in found if r <= NDCG_K)
        ideal = sum(1 / math.log2(i + 2) for i in range(min(len(ranks), NDCG_K)))
        ndcg.append(dcg / ideal if ideal else 0.0)
    count = len(answerable)
    return {
        "mrr": {
            "value": round(sum(reciprocal) / count, 6) if count else None,
            "questions": count,
            "definition": "mean 1/rank of the first expected article (0 if absent)",
        },
        "ndcg_at_10": {
            "value": round(sum(ndcg) / count, 6) if count else None,
            "questions": count,
            "definition": "mean nDCG@10 with binary gain on expected articles",
        },
    }


def summarize(
    records: dict[str, dict[str, Json]], corpus: EvalCorpus
) -> dict[str, Json]:
    """Per-mode aggregates; every rate carries its denominator."""

    def total(key: str) -> int:
        return sum(_int(r, key) for r in records.values())

    abstain = [q.question_id for q in corpus.questions if q.answer_mode == "abstain"]
    answerable = [q.question_id for q in corpus.questions if q.expected_articles]
    abstained = {
        qid
        for qid, r in records.items()
        if str(r.get("route", "")).startswith("abstained")
    }
    conflict = [
        qid
        for qid in corpus.coverage.get("conflicting_claims", ())
        if "source_conflict" in str(records[qid].get("uncertainty_categories"))
    ]
    return {
        **_ranking_quality(records, answerable),
        **{f"recall_at_{k}": _recall(records, k) for k in K_VALUES},
        "answered": ratio(
            sum(r.get("route") == "answered" for r in records.values()),
            len(records),
            "questions answered / questions",
        ),
        "citation_structural_validity": ratio(
            total("citations_structurally_valid"),
            total("citations"),
            "citations whose span is sent, current, offset-exact and bound to a claim",
        ),
        "citation_gold_article_precision": ratio(
            total("cited_expected"),
            sum(_count(r, "cited_articles") for r in records.values()),
            "distinct cited articles in the question's expected set / distinct cited",
        ),
        "citation_gold_span_exact_precision": ratio(
            total("citations_gold_span_exact"),
            total("citations"),
            "citations whose span offsets equal a gold span / citations",
        ),
        "citation_gold_span_overlap_precision": ratio(
            total("citations_gold_span_overlap"),
            total("citations"),
            "citations whose span overlaps a gold span of the same article / citations",
        ),
        "gold_span_coverage": ratio(
            total("gold_spans_covered"),
            total("gold_spans"),
            "gold spans overlapped by at least one citation / gold spans",
        ),
        "scope_compliance": ratio(
            sum(_violations(r) == 0 for r in records.values()),
            len(records),
            "questions with no out-of-scope article retrieved, sent or cited",
        ),
        "scope_violations_total": sum(_violations(r) for r in records.values()),
        "claims_verbatim_in_cited_span": ratio(
            total("claims_verbatim_in_cited_span"),
            total("claims"),
            "claims whose text is contained in a cited excerpt (copy, not entailment)",
        ),
        "abstention_correct": ratio(
            len(abstained & set(abstain)),
            len(abstain),
            "abstain questions that abstained / abstain questions",
        ),
        "false_abstention": ratio(
            len(abstained & set(answerable)),
            len(answerable),
            "answerable questions that abstained / answerable",
        ),
        "conflict_flagged": ratio(
            len(conflict),
            len(corpus.coverage.get("conflicting_claims", ())),
            "conflict questions with a source_conflict uncertainty / conflict qs",
        ),
        "temporal": {
            "query_time_basis_source_published": ratio(
                sum(
                    r.get("query_time_basis") == "source_published_at"
                    for r in records.values()
                ),
                len(records),
                "answers reporting source_published_at as the query time basis",
            ),
            "temporal_claims_emitted": total("temporal_claims"),
            "temporal_accuracy": {
                "value": None,
                "status": "NOT MEASURED",
                "reason": "fixtures/rag v1 has no gold occurrence values per question",
            },
        },
        "tokens": {
            "input_counted": total("input_tokens_counted"),
            "input_charged": total("input_tokens_charged"),
            "output_charged": total("output_tokens_charged"),
            "model_calls": total("model_calls"),
        },
        "cost_estimate": {
            "fixture_cost_total": total("cost_charged"),
            "unit": "fixture_micro_usd (synthesis-policy.v1 rates, not a price)",
        },
    }
