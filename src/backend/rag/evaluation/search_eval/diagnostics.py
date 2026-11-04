"""Layer diagnostics of pipeline benchmark traces against facet-level labels.

Inputs are text-free: the per-layer traces ``search-eval --trace-dir`` writes
(branch candidates, fused list, selection groups, payload), the label set
(grades, grade-0 judgments, filters) and the facet sidecar (required answer
facets; each facet is satisfied by any alternative, an alternative needs all
of its spans). Outputs are per-question rows and aggregates:

* article ranking (stage A): recall@k over grade-2 articles, graded nDCG@10
  and the judged share of the top-k (an unjudged article is *not* evidence of
  a miss and is never counted as grade 0 outside nDCG);
* required-facet coverage at each layer: branch candidates, fused list, top 10
  fused chunks, selection (first ``result_limit`` articles), payload. A gold
  span counts as present when the union of same-article stage spans covers
  it (code-point offsets on the same NFC text);
* attribution of each payload-missing required facet to the first layer that
  lost its best alternative, from the trace only: CANDIDATE_MISS,
  RANKING_MISS, EVIDENCE_SELECTION_MISS, BUDGET_TRUNCATION,
  SCOPE_OR_REVISION_FAILURE, else UNDETERMINED. Questions excluded by the
  manifest are LABEL_UNRESOLVED; no-answer questions are NO_ANSWER /
  FALSE_PREMISE. A complete payload is EVIDENCE_COMPLETE (generation is not
  evaluated here);
* paired differences with a cluster bootstrap over event groups (questions
  that share sources are resampled together) next to a question bootstrap.

Containment shows that the labelled text reached a layer; it does not prove a
claim. Labels are AI-reviewed candidates.
"""

import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from backend.rag.evaluation.metrics import Json, percentile, ratio
from backend.rag.evaluation.search_eval.label_audit import facet_shape_errors

GOLD_GRADE: Final = 2
NDCG_K: Final = 10
TOP_K: Final = (5, 10)
LAYERS: Final = ("branch", "fused", "top10", "selected", "payload")
BOOTSTRAP_SAMPLES: Final = 10_000
BOOTSTRAP_SEED: Final = 20261029
DIGITS: Final = 4
# Earlier layer first: the primary class of a question is its earliest loss.
CLASS_ORDER: Final = (
    "CANDIDATE_MISS",
    "RANKING_MISS",
    "EVIDENCE_SELECTION_MISS",
    "BUDGET_TRUNCATION",
    "SCOPE_OR_REVISION_FAILURE",
    "UNDETERMINED",
)

TRACE_KEYS: Final = (
    "branches",
    "fused",
    "groups",
    "result_limit",
    "max_spans_per_article",
    "payload",
    "counters",
)
# ``search-eval --trace-dir`` records the first repeat only (repeats 2+ are
# checked for identical payloads by the benchmark itself).
TRACE_REPEAT: Final = 1

type Span = tuple[str, int, int]
type Row = Mapping[str, object]


class DiagnosticsInputError(ValueError):
    """Malformed, incomplete or mismatched diagnostics input; nothing is written."""


@dataclass(frozen=True, slots=True)
class Facet:
    """One answer facet: satisfied when every span of one alternative is present."""

    facet_id: str
    required: bool
    alternatives: tuple[tuple[Span, ...], ...]


@dataclass(frozen=True, slots=True)
class QuestionLabel:
    """What the diagnostics need from one v2 question (no text)."""

    question_id: str
    split: str
    category: str
    eligible: bool
    answerable: bool
    no_answer_kind: str | None
    grades: Mapping[str, int]
    gold_spans: tuple[Span, ...]
    facets: tuple[Facet, ...]
    filters: Row
    group: str


@dataclass(frozen=True, slots=True)
class Trace:
    """One parsed per-layer trace."""

    branches: Mapping[str, tuple[Span, ...]]
    fused: tuple[Span, ...]
    groups: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    result_limit: int
    max_spans_per_article: int
    payload: tuple[Span, ...]
    truncated_payload: tuple[Span, ...]
    counters: Mapping[str, int]
    trap_hits: int

    @property
    def selected(self) -> tuple[Span, ...]:
        """Spans of the first ``result_limit`` article groups (stage B)."""
        return tuple(
            (article, start, end)
            for article, spans in self.groups[: self.result_limit]
            for start, end in spans
        )

    @property
    def article_order(self) -> list[str]:
        """Articles by first appearance in the fused list (stage A)."""
        seen: list[str] = []
        for article, _, _ in self.fused:
            if article not in seen:
                seen.append(article)
        return seen

    def layer(self, name: str) -> tuple[Span, ...]:
        """Spans present at one layer."""
        if name == "branch":
            return tuple(s for spans in self.branches.values() for s in spans)
        if name == "fused":
            return self.fused
        if name == "top10":
            return self.fused[:NDCG_K]
        if name == "selected":
            return self.selected
        return self.payload


def _ints(value: object) -> list[int]:
    return [int(cast("int", v)) for v in cast("list[object]", value)]


def parse_trace(raw: Row) -> Trace:
    """Trace JSON (as written by ``search-eval --trace-dir``) -> ``Trace``.

    Every layer key must be present (empty lists are valid: an empty payload
    is a legitimate abstention, a missing key is a broken record).
    """
    missing = [k for k in TRACE_KEYS if k not in raw]
    if missing:
        msg = f"trace misses {missing}"
        raise DiagnosticsInputError(msg)
    branches: dict[str, tuple[Span, ...]] = {}
    trap_hits = 0
    raw_branches = cast("Mapping[str, object]", raw.get("branches") or {})
    for name in ("lexical", "semantic"):
        hits = cast("list[list[object]]", raw_branches.get(name) or [])
        trap_hits += sum(1 for h in hits if h[4])
        branches[name] = tuple(
            (str(h[0]), int(cast("int", h[1])), int(cast("int", h[2])))
            for h in hits
            if not h[4]
        )
    pool = cast("list[list[object]] | None", raw_branches.get("lexical_pool"))
    if pool is not None:
        branches["lexical_pool"] = tuple(
            (str(h[0]), int(cast("int", h[1])), int(cast("int", h[2]))) for h in pool
        )
    payload_raw = cast("list[list[object]]", raw.get("payload") or [])
    return Trace(
        branches=branches,
        fused=tuple(
            (str(h[0]), int(cast("int", h[1])), int(cast("int", h[2])))
            for h in cast("list[list[object]]", raw.get("fused") or [])
        ),
        groups=tuple(
            (
                str(g[0]),
                tuple(
                    (s[0], s[1]) for s in (_ints(x) for x in cast("list[object]", g[1]))
                ),
            )
            for g in cast("list[list[object]]", raw.get("groups") or [])
        ),
        result_limit=int(cast("int", raw.get("result_limit") or 5)),
        max_spans_per_article=int(cast("int", raw.get("max_spans_per_article") or 3)),
        payload=tuple(
            (str(h[0]), int(cast("int", h[1])), int(cast("int", h[2])))
            for h in payload_raw
        ),
        truncated_payload=tuple(
            (str(h[0]), int(cast("int", h[1])), int(cast("int", h[2])))
            for h in payload_raw
            if len(h) > 3 and h[3]  # noqa: PLR2004 - [article, start, end, truncated]
        ),
        counters={
            k: int(cast("int", v))
            for k, v in cast("Mapping[str, object]", raw.get("counters") or {}).items()
        },
        trap_hits=trap_hits,
    )


def covered(span: Span, stage: Sequence[Span]) -> bool:
    """``span`` lies inside the union of same-article stage spans."""
    article, start, end = span
    intervals = sorted(
        (s, e) for a, s, e in stage if a == article and s < end and start < e
    )
    position = start
    for s, e in intervals:
        if s > position:
            return False
        position = max(position, e)
        if position >= end:
            return True
    return position >= end


def contained_in_one(span: Span, stage: Sequence[Span]) -> bool:
    """``span`` lies inside one stage span (citable as one passage)."""
    article, start, end = span
    return any(a == article and s <= start and end <= e for a, s, e in stage)


def satisfied(facet: Facet, stage: Sequence[Span]) -> bool:
    """Any alternative fully present; an empty alternative never counts."""
    return any(
        bool(alt) and all(covered(s, stage) for s in alt) for alt in facet.alternatives
    )


def _reached(alternative: tuple[Span, ...], trace: Trace) -> int:
    """Index in ``LAYERS`` of the last layer (in order) holding the alternative."""
    reached = -1
    for index, name in enumerate(LAYERS):
        if name == "top10":
            continue  # a diagnostic cut, not a pipeline layer
        if all(covered(s, trace.layer(name)) for s in alternative):
            reached = index
        else:
            break
    return reached


def _span_loss(span: Span, trace: Trace) -> tuple[str, str]:  # noqa: C901, PLR0911 - one return per layer rule
    """(class, reason) for the first layer that lost ``span``."""
    article = span[0]
    if not covered(span, trace.layer("branch")):
        if article in {a for a, _, _ in trace.layer("branch")}:
            # e.g. the PostgreSQL per-article window cap kept other windows
            return "CANDIDATE_MISS", "article_in_candidates_but_needed_chunk_not"
        return "CANDIDATE_MISS", "article_not_in_any_branch_candidate"
    if not covered(span, trace.fused):
        if article in {a for a, _, _ in trace.fused}:
            return "CANDIDATE_MISS", "needed_chunk_cut_before_fused_list"
        return "CANDIDATE_MISS", "article_cut_before_fused_list"
    if not covered(span, trace.selected):
        selected_articles = [a for a, _ in trace.groups[: trace.result_limit]]
        if article not in selected_articles:
            order = trace.article_order
            rank = order.index(article) + 1 if article in order else None
            return (
                "RANKING_MISS",
                f"article_rank_{rank}_beyond_limit_{trace.result_limit}",
            )
        spans = dict(trace.groups[: trace.result_limit])[article]
        if len(spans) >= trace.max_spans_per_article:
            return "EVIDENCE_SELECTION_MISS", "span_cap_per_article"
        return "EVIDENCE_SELECTION_MISS", "overlap_or_other_selection_rule"
    if not covered(span, trace.payload):
        if article not in {a for a, _, _ in trace.payload}:
            if trace.counters.get("dropped_at_final_check", 0):
                return "SCOPE_OR_REVISION_FAILURE", "article_dropped_at_final_check"
            return "BUDGET_TRUNCATION", "article_omitted_from_payload"
        return "BUDGET_TRUNCATION", "span_truncated_or_dropped_by_budget"
    return "UNDETERMINED", "present_but_alternative_incomplete"


def attribute(facet: Facet, trace: Trace) -> dict[str, Json]:
    """Attribution of one facet missing from the payload."""
    best = max(facet.alternatives, key=lambda alt: _reached(alt, trace))
    for span in best:
        if not covered(span, trace.payload):
            cls, reason = _span_loss(span, trace)
            return {
                "facet_id": facet.facet_id,
                "class": cls,
                "reason": reason,
                "best_alternative_layer": LAYERS[_reached(best, trace)]
                if _reached(best, trace) >= 0
                else "none",
                "article_id": span[0],
            }
    return {
        "facet_id": facet.facet_id,
        "class": "UNDETERMINED",
        "reason": "no_missing_span",
    }


def _ndcg(order: Sequence[str], grades: Mapping[str, int]) -> float | None:
    graded = {a: g for a, g in grades.items() if g > 0}
    if not graded:
        return None
    gains = sum(
        (2 ** grades.get(a, 0) - 1) / math.log2(i + 1)
        for i, a in enumerate(order[:NDCG_K], 1)
    )
    ideal = sorted(graded.values(), reverse=True)[:NDCG_K]
    best = sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(ideal, 1))
    return gains / best


def _inside(filters: Row, meta: tuple[str, datetime | None]) -> bool:
    domains = cast("list[str]", filters.get("source_domains") or [])
    domain, published = meta
    if domains and domain not in domains:
        return False
    after, before = filters.get("published_after"), filters.get("published_before")
    if after is None and before is None:
        return True
    if published is None:
        return False
    if after is not None and published < datetime.fromisoformat(str(after)):
        return False
    return before is None or published < datetime.fromisoformat(str(before))


def question_row(
    label: QuestionLabel,
    trace: Trace,
    corpus: Mapping[str, tuple[str, datetime | None]],
) -> dict[str, Json]:
    """Text-free diagnostics of one question under one configuration."""
    order = trace.article_order
    grade2 = {a for a, g in label.grades.items() if g >= GOLD_GRADE}
    payload_articles = list(dict.fromkeys(a for a, _, _ in trace.payload))
    outside = [
        a
        for a in payload_articles
        if a not in corpus or not _inside(label.filters, corpus[a])
    ]
    row: dict[str, Json] = {
        "question_id": label.question_id,
        "split": label.split,
        "category": label.category,
        "group": label.group,
        "eligible": label.eligible,
        "payload_articles": len(payload_articles),
        "payload_spans": len(trace.payload),
        "payload_tokens": trace.counters.get("payload_tokens", 0),
        "storage_calls": trace.counters.get("storage_calls", 0),
        "embedding_calls": trace.counters.get("embedding_calls", 0),
        "scope_or_stale_payload_articles": cast("list[Json]", outside),
        "trap_hits": trace.trap_hits,
        "counters": cast("dict[str, Json]", dict(trace.counters)),
    }
    for k in TOP_K:
        judged = sum(1 for a in order[:k] if a in label.grades)
        row[f"judged@{k}"] = (
            round(judged / len(order[:k]), DIGITS) if order[:k] else None
        )
    row["unjudged_payload_articles"] = cast(
        "list[Json]", [a for a in payload_articles if a not in label.grades]
    )
    if not label.answerable:
        row["class"] = (
            "FALSE_PREMISE" if label.no_answer_kind == "false_premise" else "NO_ANSWER"
        )
        return row
    for k in TOP_K:
        row[f"recall@{k}"] = round(len(grade2 & set(order[:k])) / len(grade2), DIGITS)
    ndcg = _ndcg(order, label.grades)
    row["ndcg@10"] = None if ndcg is None else round(ndcg, DIGITS)
    gold = label.gold_spans
    row["payload_gold_exact"] = sum(contained_in_one(s, trace.payload) for s in gold)
    row["payload_gold_covered"] = sum(covered(s, trace.payload) for s in gold)
    row["gold_spans"] = len(gold)
    required = [f for f in label.facets if f.required]
    for name in LAYERS:
        stage = trace.layer(name)
        hits = [satisfied(f, stage) for f in required]
        row[f"facets_{name}"] = sum(hits)
        row[f"complete_{name}"] = bool(required) and all(hits)
    row["required_facets"] = len(required)
    row["multi_facet"] = len(required) > 1
    if not label.eligible:
        row["class"] = "LABEL_UNRESOLVED"
        return row
    missing = [f for f in required if not satisfied(f, trace.payload)]
    if not missing:
        row["class"] = "EVIDENCE_COMPLETE"
        row["attribution"] = []
        return row
    found = [attribute(f, trace) for f in missing]
    row["attribution"] = cast("list[Json]", found)
    classes = {str(f["class"]) for f in found}
    row["class"] = next(c for c in CLASS_ORDER if c in classes)
    row["possible_unjudged_alternative"] = bool(row["unjudged_payload_articles"])
    return row


def _mean(values: Sequence[float], definition: str) -> dict[str, Json]:
    return {
        "value": round(sum(values) / len(values), DIGITS) if values else None,
        "samples": len(values),
        "definition": definition,
    }


def aggregate(rows: Sequence[Mapping[str, Json]]) -> dict[str, Json]:
    """Aggregates over eligible answerable rows (+ all rows for scope/size)."""
    eligible = [r for r in rows if r.get("eligible") and "recall@5" in r]
    out: dict[str, Json] = {
        "eligible_questions": len(eligible),
        "classes": dict(sorted(Counter(str(r.get("class")) for r in rows).items())),
    }
    for k in TOP_K:
        out[f"recall@{k}"] = _mean(
            [float(cast("float", r[f"recall@{k}"])) for r in eligible],
            f"grade-2 articles in top {k} of the fused article order / grade-2",
        )
        out[f"judged@{k}"] = _mean(
            [
                float(cast("float", r[f"judged@{k}"]))
                for r in eligible
                if r.get(f"judged@{k}") is not None
            ],
            f"share of top-{k} articles carrying a v2 judgment (grade 0/1/2)",
        )
    out["ndcg@10"] = _mean(
        [
            float(cast("float", r["ndcg@10"]))
            for r in eligible
            if r.get("ndcg@10") is not None
        ],
        "graded nDCG@10; unjudged articles get gain 0 here only",
    )
    required = sum(int(cast("int", r["required_facets"])) for r in eligible)
    for name in LAYERS:
        out[f"facet_coverage_{name}"] = ratio(
            sum(int(cast("int", r[f"facets_{name}"])) for r in eligible),
            required,
            f"required facets satisfied at layer {name} / required facets",
        )
        out[f"complete_{name}"] = ratio(
            sum(1 for r in eligible if r[f"complete_{name}"]),
            len(eligible),
            f"questions with every required facet at layer {name}",
        )
    multi = [r for r in eligible if r.get("multi_facet")]
    out["multi_facet_complete_payload"] = ratio(
        sum(1 for r in multi if r["complete_payload"]),
        len(multi),
        "multi-facet questions with every required facet in the payload",
    )
    out["payload_gold_exact"] = ratio(
        sum(int(cast("int", r["payload_gold_exact"])) for r in eligible),
        sum(int(cast("int", r["gold_spans"])) for r in eligible),
        "grade-2 gold spans inside one payload span / grade-2 gold spans",
    )
    out["payload_gold_covered"] = ratio(
        sum(int(cast("int", r["payload_gold_covered"])) for r in eligible),
        sum(int(cast("int", r["gold_spans"])) for r in eligible),
        "grade-2 gold spans covered by the union of payload spans / grade-2 gold spans",
    )
    out["scope_or_stale_violations"] = sum(
        len(cast("list[Json]", r["scope_or_stale_payload_articles"])) for r in rows
    )
    out["trap_hits"] = sum(int(cast("int", r["trap_hits"])) for r in rows)
    tokens = [float(cast("int", r["payload_tokens"])) for r in rows]
    out["payload_tokens"] = {
        "mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "p50": percentile(tokens, 50),
        "p95": percentile(tokens, 95),
        "total": int(sum(tokens)),
    }
    for key in (
        "payload_articles",
        "payload_spans",
        "storage_calls",
        "embedding_calls",
    ):
        values = [float(cast("int", r[key])) for r in rows]
        out[f"{key}_mean"] = round(sum(values) / len(values), 3) if values else None
    loss: Counter[str] = Counter()
    for r in eligible:
        for item in cast("list[Mapping[str, Json]]", r.get("attribution") or []):
            loss[f"{item['class']}:{item['reason']}"] += 1
    out["missing_facet_losses"] = dict(sorted(loss.items()))
    return out


def _ci(values: Sequence[float], samples: int) -> list[Json]:
    ordered = sorted(values)
    return [
        round(ordered[int(0.025 * samples)], DIGITS),
        round(ordered[min(int(0.975 * samples), samples - 1)], DIGITS),
    ]


def paired_clustered(
    diffs: Mapping[str, float],
    group_of: Mapping[str, str],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Json]:
    """Mean paired difference with question- and group-level bootstrap CIs.

    The cluster bootstrap resamples event groups with replacement and takes
    the mean over all questions of the drawn groups, so questions sharing a
    source never count as independent draws.
    """
    ids = sorted(diffs)
    if not ids:
        return {"n": 0}
    values = [diffs[q] for q in ids]
    groups: dict[str, list[float]] = {}
    for q in ids:
        groups.setdefault(group_of.get(q, q), []).append(diffs[q])
    names = sorted(groups)
    rng = random.Random(seed)  # noqa: S311 - reproducible statistics, not crypto
    question_means = [
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(samples)
    ]
    rng = random.Random(seed)  # noqa: S311 - reproducible statistics, not crypto
    cluster_means: list[float] = []
    for _ in range(samples):
        drawn = [groups[rng.choice(names)] for _ in names]
        total = sum(sum(g) for g in drawn)
        count = sum(len(g) for g in drawn)
        cluster_means.append(total / count)
    cluster = _ci(cluster_means, samples)
    return {
        "n": len(ids),
        "groups": len(names),
        "mean_diff": round(sum(values) / len(values), DIGITS),
        "ci95_question_bootstrap": _ci(question_means, samples),
        "ci95_cluster_bootstrap": cluster,
        "cluster_ci_excludes_zero": cast("float", cluster[0]) > 0
        or cast("float", cluster[1]) < 0,
        "wins": sum(v > 0 for v in values),
        "losses": sum(v < 0 for v in values),
        "ties": sum(v == 0 for v in values),
        "bootstrap": {"samples": samples, "seed": seed},
    }


def _span(ref: Row) -> Span:
    return (
        str(ref["article_id"]),
        int(cast("int", ref["start"])),
        int(cast("int", ref["end"])),
    )


def load_labels(questions: Row, facets: Row, manifest: Row) -> dict[str, QuestionLabel]:
    """v2 label set + facet sidecar + frozen evaluation manifest -> labels."""
    eligible = set(cast("list[str]", manifest.get("eligible_answerable") or []))
    groups = cast(
        "Mapping[str, str]",
        cast("Row", manifest.get("event_groups") or {}).get("question_to_group") or {},
    )
    by_question = cast("Mapping[str, Row]", facets.get("questions") or {})
    out: dict[str, QuestionLabel] = {}
    for row in cast("list[Row]", questions.get("questions") or []):
        qid = str(row["qid"])
        grades = {
            str(g["article_id"]): int(cast("int", g["grade"]))
            for g in cast("list[Row]", row.get("gold") or [])
        }
        for judged in cast("list[Row]", row.get("judged_nonrelevant") or []):
            _ = grades.setdefault(str(judged["article_id"]), 0)
        gold = tuple(
            (
                str(g["article_id"]),
                int(cast("int", s["start"])),
                int(cast("int", s["end"])),
            )
            for g in cast("list[Row]", row.get("gold") or [])
            if int(cast("int", g["grade"])) >= GOLD_GRADE
            for s in cast("list[Row]", g["spans"])
        )
        entry: Row | None = by_question.get(qid)
        if entry is None:
            if qid in eligible:
                msg = f"{qid}: eligible question without facets"
                raise DiagnosticsInputError(msg)
            entry = cast("Row", {"facets": cast("list[object]", [])})
        shape = facet_shape_errors(entry)
        if shape:
            msg = f"{qid}: malformed facets {shape[:3]}"
            raise DiagnosticsInputError(msg)
        facet_list = tuple(
            Facet(
                facet_id=str(f["facet_id"]),
                required=bool(f.get("required")),
                alternatives=tuple(
                    tuple(_span(ref) for ref in alt)
                    for alt in cast("list[list[Row]]", f.get("alternatives") or [])
                ),
            )
            for f in cast("list[Row]", entry.get("facets") or [])
        )
        kind = row.get("no_answer_kind")
        out[qid] = QuestionLabel(
            question_id=qid,
            split=str(row.get("split")),
            category=str(row.get("category")),
            eligible=qid in eligible,
            answerable=bool(row.get("answerable")),
            no_answer_kind=None if kind is None else str(kind),
            grades=grades,
            gold_spans=gold,
            facets=facet_list,
            filters=cast("Row", row.get("filters") or {}),
            group=str(groups.get(qid, qid)),
        )
    return out


PAIR_METRICS: Final = (
    "complete_payload",
    "facet_share_payload",
    "complete_selected",
    "complete_fused",
    "recall@5",
    "recall@10",
    "ndcg@10",
    "payload_tokens",
)


def _metric(row: Mapping[str, Json], metric: str) -> float | None:
    if metric == "facet_share_payload":
        required = int(cast("int", row.get("required_facets") or 0))
        if not required:
            return None
        return int(cast("int", row["facets_payload"])) / required
    value = row.get(metric)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return None


def compare_pairs(
    rows: Mapping[str, Mapping[str, Mapping[str, Json]]],
    pairs: Sequence[tuple[str, str]],
    labels: Mapping[str, QuestionLabel],
) -> list[Json]:
    """Paired differences (candidate - baseline) on eligible questions."""
    out: list[Json] = []
    group_of = {q: label.group for q, label in labels.items()}
    for baseline, candidate in pairs:
        if baseline not in rows or candidate not in rows:
            continue
        left, right = rows[baseline], rows[candidate]
        result: dict[str, Json] = {"baseline": baseline, "candidate": candidate}
        for metric in PAIR_METRICS:
            diffs: dict[str, float] = {}
            for qid, label in labels.items():
                if not label.eligible or qid not in left or qid not in right:
                    continue
                a, b = _metric(left[qid], metric), _metric(right[qid], metric)
                if a is not None and b is not None:
                    diffs[qid] = b - a
            result[metric] = paired_clustered(diffs, group_of)
        out.append(result)
    return out


def _record(
    number: int, line: str, wanted: set[str], labels: Mapping[str, QuestionLabel]
) -> tuple[str, str, Row]:
    """One trace line -> (configuration, question, raw trace) or an error."""
    try:
        item = cast("object", json.loads(line))
    except json.JSONDecodeError as error:
        msg = f"line {number}: invalid JSON"
        raise DiagnosticsInputError(msg) from error
    if not isinstance(item, Mapping):
        msg = f"line {number}: record is not an object"
        raise DiagnosticsInputError(msg)
    fields = cast("Row", item)
    if not {"configuration", "question_id", "trace"} <= set(fields):
        msg = f"line {number}: record misses configuration/question_id/trace"
        raise DiagnosticsInputError(msg)
    name, qid = str(fields["configuration"]), str(fields["question_id"])
    problem = None
    if name not in wanted:
        problem = f"configuration {name!r} not in the manifest"
    elif qid not in labels:
        problem = f"unknown question {qid!r}"
    elif fields.get("repeat", TRACE_REPEAT) != TRACE_REPEAT:
        problem = f"repeat {fields.get('repeat')!r} != {TRACE_REPEAT}"
    elif not isinstance(fields["trace"], Mapping):
        problem = "trace is not an object"
    if problem is not None:
        msg = f"line {number}: {problem}"
        raise DiagnosticsInputError(msg)
    return name, qid, cast("Row", fields["trace"])


def collect_traces(
    lines: Sequence[str],
    labels: Mapping[str, QuestionLabel],
    configurations: Sequence[str],
) -> dict[str, dict[str, Trace]]:
    """Validated traces: exactly one record per (configuration, question).

    Rejects unknown configurations or questions, duplicates, a ``repeat``
    other than the recorded first repeat, malformed records and any missing
    pair. A question whose search raised is never written by the benchmark,
    so it surfaces here as missing instead of being silently skipped.
    """
    wanted = set(configurations)
    if not wanted:
        msg = "manifest names no configurations"
        raise DiagnosticsInputError(msg)
    out: dict[str, dict[str, Trace]] = {name: {} for name in configurations}
    for number, line in enumerate(lines, 1):
        name, qid, raw = _record(number, line, wanted, labels)
        if qid in out[name]:
            msg = f"line {number}: duplicate record {name} / {qid}"
            raise DiagnosticsInputError(msg)
        try:
            out[name][qid] = parse_trace(raw)
        except (TypeError, IndexError, KeyError) as error:
            msg = f"line {number}: malformed trace ({error})"
            raise DiagnosticsInputError(msg) from error
    missing = [
        f"{name}/{qid}"
        for name in configurations
        for qid in sorted(labels)
        if qid not in out[name]
    ]
    if missing:
        msg = f"{len(missing)} records missing, e.g. {missing[:3]}"
        raise DiagnosticsInputError(msg)
    return out


def check_identities(plan: Row, inputs: Mapping[str, bytes]) -> None:
    """Every input digest named by the frozen manifest must match the bytes."""
    for key, data in inputs.items():
        actual = hashlib.sha256(data).hexdigest()
        if plan.get(key) != actual:
            msg = f"{key}: manifest {plan.get(key)!r} != input {actual}"
            raise DiagnosticsInputError(msg)
