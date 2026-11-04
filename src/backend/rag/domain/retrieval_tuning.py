"""Opt-in retrieval tuning: branch weights, evidence diversity, intent freshness.

Framework-free. ``BASELINE_TUNING`` reproduces the frozen ``retrieval-policy.v3``
behaviour exactly (equal RRF weights, 3 spans per article, no overlap filter,
article-major packing, no index-path per-article cap, v3 branch freshness), so
the production default is unchanged until a profile is selected explicitly.

Definitions (all tested in ``tests/backend/rag/unit/test_retrieval_tuning.py``):

* Weighted RRF: ``score(d) = w_lex / (k + rank_lex(d)) + w_sem / (k + rank_sem(d))``;
  a branch without the chunk contributes 0. A zero weight *disables* the branch:
  it is not executed (no embedding call for semantic) and a chunk found only by
  it can never enter the fused list.
* Span overlap: two spans of the same article and content hash overlap by
  ``ratio = |[s1,e1) ∩ [s2,e2)| / min(e1 - s1, e2 - s2)``. With a threshold
  ``t`` a later-ranked span with ``ratio >= t`` against an already kept span of
  the same article is dropped (the next-ranked span of that article may take
  its slot). Spans of different articles are never compared here (cross-article
  near duplicates stay with the v3 shingle dedup).
* Intent freshness (``mode = "intent"``): only for intents listed in
  ``intents`` and only when the request has no explicit publication window.
  Each fused candidate gets

      bonus = W * (1 / (k + 1) - 1 / (k + 1 + R)) * 0.5 ** (age_days / H)

  with ``W`` the sum of the weights of the branches that returned candidates,
  ``R = max_rank_shift`` and ``H = half_life_days``. The bonus is on the RRF
  scale (never a raw branch score). RRF gaps shrink down the list, so the
  bonus alone does not bound movement; ``fusion.apply_intent_freshness``
  therefore enforces on the final order that no candidate moves up more than
  ``R`` positions relative to the baseline fused order (deterministic,
  same candidate set). The guard bounds promotion only: it does not show that
  a promoted chunk is relevant, so a new off-topic chunk can still move up to
  ``R`` positions. Missing or future ``source_published_at`` (relative to the
  injected query clock) get 0; no other timestamp is substituted.
"""

# ruff: noqa: EM101, EM102, TRY003 - one-line validation messages

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Final, Literal, cast
from uuid import UUID

TUNING_SCHEMA: Final = "retrieval-tuning.v1"
BASELINE_TUNING_ID: Final = "baseline"
MAX_SPANS_PER_ARTICLE: Final = 10
MAX_INDEX_DEPTH: Final = 100
MAX_RANK_SHIFT: Final = 20
SECONDS_PER_DAY: Final = 86_400.0

type Packing = Literal["article_major", "breadth_first"]
type FreshnessMode = Literal["policy", "intent"]


class InvalidTuningError(ValueError):
    """A tuning value is out of range or inconsistent."""

    def __init__(self, detail: str) -> None:
        """Name the violated rule."""
        super().__init__(f"invalid retrieval tuning: {detail}")


def _finite_non_negative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidTuningError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise InvalidTuningError(f"{name} must be finite and >= 0")


@dataclass(frozen=True, slots=True)
class FusionWeights:
    """Per-branch RRF weights (k stays the policy's 60)."""

    lexical: float = 1.0
    semantic: float = 1.0

    def __post_init__(self) -> None:
        """Finite, non-negative, not both zero."""
        _finite_non_negative("lexical weight", self.lexical)
        _finite_non_negative("semantic weight", self.semantic)
        if self.lexical == 0 and self.semantic == 0:
            raise InvalidTuningError("lexical and semantic weights are both 0")

    @property
    def lexical_enabled(self) -> bool:
        """A zero weight turns the branch off (not executed, not fused)."""
        return self.lexical > 0

    @property
    def semantic_enabled(self) -> bool:
        """A zero weight turns the branch off (no query embedding)."""
        return self.semantic > 0


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Article/passage selection and packing after fusion.

    ``max_spans_per_article`` ``None`` keeps the policy's aggregation cap (v3:
    3). ``overlap_threshold`` ``None`` keeps every span (v3 behaviour).
    ``fill_spans_per_article`` (``None`` = off) is a second, lower-priority
    pass: further spans of already selected articles, up to that total per
    article, marked as *fill*; packing sends every base span first and adds
    fill spans only into budget that is left, never displacing a base span or
    another source.
    """

    max_spans_per_article: int | None = None
    overlap_threshold: float | None = None
    packing: Packing = "article_major"
    fill_spans_per_article: int | None = None

    def __post_init__(self) -> None:
        """Bounded caps, threshold in (0, 1], known packing."""
        cap = self.max_spans_per_article
        if cap is not None and not 1 <= cap <= MAX_SPANS_PER_ARTICLE:
            raise InvalidTuningError(
                f"max_spans_per_article must be in 1..{MAX_SPANS_PER_ARTICLE}"
            )
        fill = self.fill_spans_per_article
        if fill is not None and not 1 <= fill <= MAX_SPANS_PER_ARTICLE:
            raise InvalidTuningError(
                f"fill_spans_per_article must be in 1..{MAX_SPANS_PER_ARTICLE}"
            )
        threshold = self.overlap_threshold
        if threshold is not None:
            _finite_non_negative("overlap_threshold", threshold)
            if not 0 < threshold <= 1:
                raise InvalidTuningError("overlap_threshold must be in (0, 1]")
        if self.packing not in {"article_major", "breadth_first"}:
            raise InvalidTuningError("packing must be article_major|breadth_first")


@dataclass(frozen=True, slots=True)
class BranchDiversity:
    """Per-article cap on each index branch, with one bounded backfill call.

    ``max_chunks_per_article`` ``None`` = off (v3 index path). A backfill
    re-queries a branch once at ``backfill_depth`` only when the cap removed
    hits, the branch returned its full depth and at least
    ``min_remaining_seconds`` remain before the query deadline.
    """

    max_chunks_per_article: int | None = None
    backfill_depth: int | None = None
    max_backfill_calls: int = 1
    min_remaining_seconds: float = 1.0

    def __post_init__(self) -> None:
        """Cap and depths bounded; backfill needs a cap."""
        cap = self.max_chunks_per_article
        if cap is not None and not 1 <= cap <= MAX_SPANS_PER_ARTICLE:
            raise InvalidTuningError(
                f"max_chunks_per_article must be in 1..{MAX_SPANS_PER_ARTICLE}"
            )
        depth = self.backfill_depth
        if depth is not None:
            if cap is None:
                raise InvalidTuningError("backfill needs max_chunks_per_article")
            if not 1 <= depth <= MAX_INDEX_DEPTH:
                raise InvalidTuningError(
                    f"backfill_depth must be in 1..{MAX_INDEX_DEPTH}"
                )
        if self.max_backfill_calls not in {0, 1}:
            raise InvalidTuningError("max_backfill_calls must be 0 or 1")
        _finite_non_negative("min_remaining_seconds", self.min_remaining_seconds)


@dataclass(frozen=True, slots=True)
class IntentFreshness:
    """``policy`` = v3 branch freshness unchanged; ``intent`` = rules above."""

    mode: FreshnessMode = "policy"
    half_life_days: float = 7.0
    max_rank_shift: int = 5
    intents: tuple[str, ...] = ("update",)

    def __post_init__(self) -> None:
        """Positive half-life; rank shift bounded (1..MAX_RANK_SHIFT)."""
        if self.mode not in {"policy", "intent"}:
            raise InvalidTuningError("freshness mode must be policy|intent")
        _finite_non_negative("half_life_days", self.half_life_days)
        if self.half_life_days <= 0:
            raise InvalidTuningError("half_life_days must be > 0")
        shift = cast("object", self.max_rank_shift)
        if (
            isinstance(shift, bool)
            or not isinstance(shift, int)
            or not 1 <= shift <= MAX_RANK_SHIFT
        ):
            raise InvalidTuningError(
                f"max_rank_shift must be an int in 1..{MAX_RANK_SHIFT}"
            )


@dataclass(frozen=True, slots=True)
class FieldBoosts:
    """BM25 field boosts title:sapo:content (index path only)."""

    title: float = 3.0
    sapo: float = 1.5
    text: float = 1.0

    def __post_init__(self) -> None:
        """Finite and positive."""
        for name, value in (
            ("title", self.title),
            ("sapo", self.sapo),
            ("text", self.text),
        ):
            _finite_non_negative(f"{name} boost", value)
            if value <= 0:
                raise InvalidTuningError(f"{name} boost must be > 0")


@dataclass(frozen=True, slots=True)
class RetrievalTuning:
    """One named, fully explicit tuning; the ID is recorded in provenance."""

    tuning_id: str = BASELINE_TUNING_ID
    weights: FusionWeights = field(default_factory=FusionWeights)
    selection: SelectionPolicy = field(default_factory=SelectionPolicy)
    diversity: BranchDiversity = field(default_factory=BranchDiversity)
    freshness: IntentFreshness = field(default_factory=IntentFreshness)
    boosts: FieldBoosts = field(default_factory=FieldBoosts)

    def __post_init__(self) -> None:
        """Non-empty ID."""
        if not self.tuning_id.strip():
            raise InvalidTuningError("tuning_id must not be empty")

    def describe(self) -> dict[str, object]:
        """Every effective value (report/checkpoint identity)."""
        values = asdict(self)
        values["schema"] = TUNING_SCHEMA
        return values


BASELINE_TUNING: Final = RetrievalTuning()


def _section[T](cls: type[T], raw: object, name: str) -> T:
    if raw is None:
        return cls()
    if not isinstance(raw, Mapping):
        raise InvalidTuningError(f"{name} must be an object")
    values: dict[str, object] = {
        str(key): value for key, value in cast("Mapping[object, object]", raw).items()
    }
    intents = values.get("intents")
    if isinstance(intents, list):
        values["intents"] = tuple(cast("list[object]", intents))
    try:
        return cls(**values)
    except TypeError as error:
        raise InvalidTuningError(f"{name}: {error}") from None


def parse_tuning(tuning_id: str, raw: Mapping[str, object]) -> RetrievalTuning:
    """Build a tuning from a profile object; unknown keys are refused."""
    known = {"weights", "selection", "diversity", "freshness", "boosts", "note"}
    unknown = set(raw) - known
    if unknown:
        raise InvalidTuningError(f"unknown keys {sorted(unknown)}")
    weights = raw.get("weights")
    if isinstance(weights, Sequence) and not isinstance(weights, str | Mapping):
        if len(weights) != 2:  # noqa: PLR2004 - [lexical, semantic]
            raise InvalidTuningError("weights must be [lexical, semantic]")
        weights = {"lexical": weights[0], "semantic": weights[1]}
    return RetrievalTuning(
        tuning_id=tuning_id,
        weights=_section(FusionWeights, weights, "weights"),
        selection=_section(SelectionPolicy, raw.get("selection"), "selection"),
        diversity=_section(BranchDiversity, raw.get("diversity"), "diversity"),
        freshness=_section(IntentFreshness, raw.get("freshness"), "freshness"),
        boosts=_section(FieldBoosts, raw.get("boosts"), "boosts"),
    )


def overlap_ratio(left: tuple[int, int], right: tuple[int, int]) -> float:
    """Shared code points / length of the shorter span (0 for empty spans)."""
    shortest = min(left[1] - left[0], right[1] - right[0])
    if shortest <= 0:
        return 0.0
    shared = min(left[1], right[1]) - max(left[0], right[0])
    return max(0, shared) / shortest


@dataclass(frozen=True, slots=True)
class SpanKey:
    """What passage selection needs to know about one ranked chunk."""

    article_id: UUID
    content_hash: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SpanSelection:
    """Selected positions grouped by article (best-chunk order) plus counts."""

    groups: tuple[tuple[UUID, tuple[int, ...]], ...]
    overlap_dropped: int
    cap_dropped: int
    fill: frozenset[int] = frozenset()


def _overlaps(
    keys: Sequence[SpanKey], kept: Sequence[int], key: SpanKey, threshold: float
) -> bool:
    return any(
        keys[k].content_hash == key.content_hash
        and overlap_ratio((keys[k].start, keys[k].end), (key.start, key.end))
        >= threshold
        for k in kept
    )


def select_spans(
    keys: Sequence[SpanKey],
    max_per_article: int,
    overlap_threshold: float | None,
    fill_per_article: int | None = None,
) -> SpanSelection:
    """Group ranked chunks by article; keep the best ``max_per_article`` each.

    Articles are ordered by the position of their best chunk (never by a sum
    over chunks, so long articles gain nothing from having more chunks);
    within an article spans stay in rank order. With a threshold, a span that
    overlaps an already kept span of the same article/revision by at least the
    threshold is dropped and a later span may take its slot.
    """
    groups: dict[UUID, list[int]] = {}
    overlap_dropped = 0
    capped: list[int] = []
    for position, key in enumerate(keys):
        bucket = groups.setdefault(key.article_id, [])
        if overlap_threshold is not None and _overlaps(
            keys, bucket, key, overlap_threshold
        ):
            overlap_dropped += 1
            continue
        if len(bucket) >= max_per_article:
            capped.append(position)
            continue
        bucket.append(position)
    fill: set[int] = set()
    if fill_per_article is not None and fill_per_article > max_per_article:
        for position in capped:
            key = keys[position]
            bucket = groups[key.article_id]
            if len(bucket) >= fill_per_article or (
                overlap_threshold is not None
                and _overlaps(keys, bucket, key, overlap_threshold)
            ):
                continue
            bucket.append(position)
            fill.add(position)
    return SpanSelection(
        groups=tuple((a, tuple(sorted(items))) for a, items in groups.items() if items),
        overlap_dropped=overlap_dropped,
        cap_dropped=len(capped) - len(fill),
        fill=frozenset(fill),
    )


def cap_per_article[T](
    items: Sequence[T], article_of: Callable[[T], UUID], cap: int
) -> tuple[tuple[T, ...], int]:
    """Keep at most ``cap`` items per article in order; returns (kept, dropped)."""
    taken: dict[UUID, int] = {}
    kept: list[T] = []
    dropped = 0
    for item in items:
        article = article_of(item)
        count = taken.get(article, 0)
        if count >= cap:
            dropped += 1
            continue
        taken[article] = count + 1
        kept.append(item)
    return tuple(kept), dropped


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    """Whether intent freshness ran and why (text-free, for metrics)."""

    applied: bool
    reason: str
    boosted: int = 0
    future_timestamps: int = 0
    missing_timestamps: int = 0
    max_promotion: int = 0


def freshness_decision(
    policy: IntentFreshness, intent: str | None, *, explicit_window: bool
) -> FreshnessDecision:
    """Gate: mode, explicit publication window, then intent."""
    if policy.mode != "intent":
        return FreshnessDecision(applied=False, reason="policy_mode")
    if explicit_window:
        return FreshnessDecision(applied=False, reason="explicit_window")
    if intent is None or intent not in policy.intents:
        return FreshnessDecision(applied=False, reason="intent_not_eligible")
    return FreshnessDecision(applied=True, reason="applied")


def freshness_bonus(
    published: datetime | None,
    now: datetime,
    policy: IntentFreshness,
    k: int,
    weight_sum: float = 1.0,
) -> float:
    """``W * (1/(k+1) - 1/(k+1+R)) * 0.5 ** (age / H)``; 0 if missing or future."""
    if published is None or published > now:
        return 0.0
    age_days = (now - published).total_seconds() / SECONDS_PER_DAY
    shift = 1.0 / (k + 1) - 1.0 / (k + 1 + policy.max_rank_shift)
    return weight_sum * shift * math.pow(0.5, age_days / policy.half_life_days)


def with_tuning_id(tuning: RetrievalTuning, tuning_id: str) -> RetrievalTuning:
    """Same values under another name (profiles that extend another)."""
    return replace(tuning, tuning_id=tuning_id)


__all__ = [
    "BASELINE_TUNING",
    "BASELINE_TUNING_ID",
    "TUNING_SCHEMA",
    "BranchDiversity",
    "FieldBoosts",
    "FreshnessDecision",
    "FusionWeights",
    "IntentFreshness",
    "InvalidTuningError",
    "RetrievalTuning",
    "SelectionPolicy",
    "SpanKey",
    "SpanSelection",
    "cap_per_article",
    "freshness_bonus",
    "freshness_decision",
    "overlap_ratio",
    "parse_tuning",
    "select_spans",
    "with_tuning_id",
]
