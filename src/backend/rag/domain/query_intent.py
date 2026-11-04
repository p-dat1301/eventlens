"""Deterministic query-intent classification and bounded multi-hop policy.

No model is involved: intents come from versioned cue lists (Vietnamese and
English) over the casefolded NFC query. The classifier only decides whether a
second, bounded retrieval hop may run; it never rewrites filters or access.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from unicodedata import normalize

INTENT_CLASSIFIER_VERSION: Final = "query-intent-rules.v1"
HOP_POLICY_VERSION: Final = "bounded-hops.v1"
_WORD = re.compile(r"\w+")


class QueryIntent(StrEnum):
    """Supported intents; ``factual`` is the default."""

    FACTUAL = "factual"
    NUMERIC = "numeric"
    TEMPORAL = "temporal"
    UPDATE = "update"
    COMPARISON = "comparison"
    MULTI_SOURCE = "multi_source"
    NO_ANSWER_CANDIDATE = "no_answer_candidate"


# Ordered by priority: the first intent with a matching cue wins.
_CUES: Final[tuple[tuple[QueryIntent, tuple[str, ...]], ...]] = (
    (
        QueryIntent.NO_ANSWER_CANDIDATE,
        (
            r"giả sử",
            r"nếu như",
            r"what if",
            r"would happen",
            r"liệu có thể sẽ",
        ),
    ),
    (
        QueryIntent.COMPARISON,
        (
            r"so sánh",
            r"so với",
            r"khác nhau",
            r"khác gì",
            r"compare[ds]?",
            r"comparison",
            r"versus",
            r"vs",
            r"difference between",
        ),
    ),
    (
        QueryIntent.UPDATE,
        (
            r"mới nhất",
            r"cập nhật",
            r"hiện nay",
            r"hiện tại",
            r"bây giờ",
            r"vẫn còn",
            r"tiến độ",
            r"diễn biến",
            r"latest",
            r"update[ds]?",
            r"still",
            r"currently",
            r"now",
            r"most recent",
        ),
    ),
    (
        QueryIntent.MULTI_SOURCE,
        (
            r"các nguồn",
            r"nhiều nguồn",
            r"các báo",
            r"các bên",
            r"tổng hợp",
            r"phản ứng",
            r"sources",
            r"according to (?:different|various|multiple)",
            r"reactions?",
            r"stakeholders",
        ),
    ),
    (
        QueryIntent.NUMERIC,
        (
            r"bao nhiêu",
            r"mấy",
            r"tỷ lệ",
            r"phần trăm",
            r"số lượng",
            r"how many",
            r"how much",
            r"percent(?:age)?",
            r"\d+(?:[.,]\d+)?\s*%",
        ),
    ),
    (
        QueryIntent.TEMPORAL,
        (
            r"khi nào",
            r"bao giờ",
            r"lúc nào",
            r"ngày nào",
            r"thời điểm",
            r"when",
            r"what date",
            r"(?:19|20)\d{2}",
            r"tháng \d{1,2}",
            r"ngày \d{1,2}",
        ),
    ),
)
_PATTERNS: Final = tuple(
    (intent, re.compile(r"(?<!\w)(?:" + "|".join(cues) + r")(?!\w)"))
    for intent, cues in _CUES
)
# Fewer words (Vietnamese syllables count) is too vague to answer from evidence.
_MIN_TERMS: Final = 3
SECOND_HOP_INTENTS: Final = frozenset({QueryIntent.UPDATE, QueryIntent.MULTI_SOURCE})


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """Intent plus the cue that selected it (for traces, no query text)."""

    intent: QueryIntent
    matched_cue: str | None
    classifier_version: str = INTENT_CLASSIFIER_VERSION


def _folded(query: str) -> str:
    return normalize("NFC", query).casefold()


def classify_intent(query: str) -> IntentDecision:
    """Classify one query with the frozen cue lists (pure, deterministic)."""
    folded = _folded(query)
    if sum(1 for _ in _WORD.finditer(folded)) < _MIN_TERMS:
        return IntentDecision(QueryIntent.NO_ANSWER_CANDIDATE, "too_few_terms")
    for intent, pattern in _PATTERNS:
        match = pattern.search(folded)
        if match is not None:
            return IntentDecision(intent, match.group(0))
    return IntentDecision(QueryIntent.FACTUAL, None)


class HopStopReason(StrEnum):
    """Why no (further) hop runs; ``allowed`` when the hop runs."""

    ALLOWED = "allowed"
    DISABLED = "disabled"
    INTENT_NOT_ELIGIBLE = "intent_not_eligible"
    NO_PRIMARY_RESULTS = "no_primary_results"
    MAX_HOPS_REACHED = "max_hops_reached"
    MAX_EXPANSIONS_REACHED = "max_expansions_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_NEAR = "deadline_near"


@dataclass(frozen=True, slots=True)
class HopPolicy:
    """Hard bounds: 2 retrieval hops, 1 query expansion, 1 rerank, no loops.

    ``enabled`` is off by default: hop 2 changed fixture answers without a
    measured gain (``docs/query-intents.md``), so it is opt-in
    (``EVENTLENS_RAG_MULTI_HOP=on``) until a benchmark shows improvement.
    """

    enabled: bool = False
    max_hops: int = 2
    max_expansions: int = 1
    max_reranks: int = 1
    min_remaining_seconds: float = 8.0
    max_expansion_terms: int = 16
    version: str = HOP_POLICY_VERSION


DEFAULT_HOP_POLICY: Final = HopPolicy()


@dataclass(frozen=True, slots=True)
class HopTrace:
    """One recorded hop (counts and timing only)."""

    hop: int
    kind: str
    candidates: int
    latency_ms: float
    decision: HopStopReason


def second_hop_decision(  # noqa: PLR0913, PLR0911 - one exit per bound
    intent: QueryIntent,
    *,
    primary_candidates: int,
    hops_done: int,
    expansions_done: int,
    remaining_seconds: float | None,
    budget_remaining: int | None = None,
    policy: HopPolicy = DEFAULT_HOP_POLICY,
) -> HopStopReason:
    """Whether the bounded second hop may run, else the first failed bound."""
    if not policy.enabled:
        return HopStopReason.DISABLED
    if intent not in SECOND_HOP_INTENTS:
        return HopStopReason.INTENT_NOT_ELIGIBLE
    if primary_candidates <= 0:
        return HopStopReason.NO_PRIMARY_RESULTS
    if hops_done >= policy.max_hops:
        return HopStopReason.MAX_HOPS_REACHED
    if expansions_done >= policy.max_expansions:
        return HopStopReason.MAX_EXPANSIONS_REACHED
    if budget_remaining is not None and budget_remaining <= 0:
        return HopStopReason.BUDGET_EXHAUSTED
    if (
        remaining_seconds is not None
        and remaining_seconds < policy.min_remaining_seconds
    ):
        return HopStopReason.DEADLINE_NEAR
    return HopStopReason.ALLOWED


def expansion_query(
    query: str, anchor_texts: tuple[str, ...], policy: HopPolicy = DEFAULT_HOP_POLICY
) -> str:
    """One deterministic expansion: the query plus new terms of anchor texts."""
    terms: list[str] = [m.group(0) for m in _WORD.finditer(_folded(query))]
    seen = set(terms)
    for anchor in anchor_texts:
        for match in _WORD.finditer(_folded(anchor)):
            term = match.group(0)
            if term not in seen and len(terms) < policy.max_expansion_terms:
                seen.add(term)
                terms.append(term)
    return " ".join(terms)
