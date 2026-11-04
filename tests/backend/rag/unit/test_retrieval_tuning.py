"""Pure retrieval tuning: weighted RRF, passage selection, intent freshness."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import Random
from uuid import UUID, uuid4

import pytest

from backend.rag.application.retrieval_profiles import (
    bundled_profiles,
    bundled_tuning,
    load_profiles,
)
from backend.rag.application.use_cases.retrieve import bundled_retrieval_policy
from backend.rag.domain.fusion import apply_intent_freshness, fuse, suppress_duplicates
from backend.rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    ChunkRef,
    FusedCandidate,
)
from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    BranchDiversity,
    FieldBoosts,
    FreshnessDecision,
    FusionWeights,
    IntentFreshness,
    InvalidTuningError,
    SelectionPolicy,
    SpanKey,
    cap_per_article,
    freshness_bonus,
    overlap_ratio,
    parse_tuning,
    select_spans,
)

NOW = datetime(2026, 10, 28, tzinfo=UTC)
K = 60
A = UUID("00000000-0000-0000-0000-00000000000a")
B = UUID("00000000-0000-0000-0000-00000000000b")
C = UUID("00000000-0000-0000-0000-00000000000c")


def _chunk(article: UUID, index: int, start: int = 0, end: int = 100) -> ChunkRef:
    return ChunkRef(
        article, f"sha256:{article.hex}", "paragraph-900-1.v1", index, start, end
    )


def _branch(chunk: ChunkRef, branch: Branch, rank: int) -> BranchCandidate:
    return BranchCandidate(
        chunk=chunk,
        branch=branch,
        rank=rank,
        score=1.0,
        source_published_at=None,
        text=f"text {chunk.article_id} {chunk.chunk_index}",
        vector_generation=None,
    )


# --- weighted RRF -------------------------------------------------------------


def test_weighted_rrf_matches_the_documented_formula() -> None:
    shared, lex_only, vec_only = _chunk(A, 0), _chunk(B, 0), _chunk(C, 0)
    lexical = (_branch(shared, Branch.LEXICAL, 1), _branch(lex_only, Branch.LEXICAL, 2))
    vector = (_branch(vec_only, Branch.VECTOR, 1), _branch(shared, Branch.VECTOR, 3))

    fused = {f.chunk.chunk_id: f for f in fuse(lexical, vector, K, FusionWeights(1, 2))}

    assert math.isclose(fused[shared.chunk_id].fused_score, 1 / 61 + 2 / 63)
    assert math.isclose(fused[lex_only.chunk_id].fused_score, 1 / 62)
    assert math.isclose(fused[vec_only.chunk_id].fused_score, 2 / 61)
    assert fused[vec_only.chunk_id].lexical_rank is None  # missing branch adds 0


def test_equal_weights_reproduce_the_v3_fusion_exactly() -> None:
    lexical = tuple(_branch(_chunk(A, i), Branch.LEXICAL, i + 1) for i in range(4))
    vector = tuple(_branch(_chunk(B, i), Branch.VECTOR, i + 1) for i in range(4))

    assert fuse(lexical, vector, K) == fuse(lexical, vector, K, FusionWeights(1, 1))


@pytest.mark.parametrize(("weights", "kept"), [((1, 0), A), ((0, 1), B)])
def test_zero_weight_disables_the_branch_and_its_only_candidates(
    weights: tuple[float, float], kept: UUID
) -> None:
    lexical = (_branch(_chunk(A, 0), Branch.LEXICAL, 1),)
    vector = (_branch(_chunk(B, 0), Branch.VECTOR, 1),)

    fused = fuse(lexical, vector, K, FusionWeights(*weights))

    assert [f.chunk.article_id for f in fused] == [kept]


def test_weighted_ties_break_deterministically_on_ids() -> None:
    first, second = _chunk(A, 0), _chunk(B, 0)
    lexical = (_branch(first, Branch.LEXICAL, 1),)
    vector = (_branch(second, Branch.VECTOR, 1),)

    once = fuse(lexical, vector, K, FusionWeights(1, 1))
    again = fuse(tuple(reversed(lexical)), vector, K, FusionWeights(1, 1))

    assert [f.chunk.article_id for f in once] == [A, B]  # equal score -> article_id
    assert once == again


@pytest.mark.parametrize(
    "weights",
    [(0, 0), (-1, 1), (1, math.inf), (math.nan, 1), (True, 1)],
)
def test_invalid_weights_are_refused(weights: tuple[float, float]) -> None:
    with pytest.raises(InvalidTuningError):
        _ = FusionWeights(*weights)


# --- passage selection --------------------------------------------------------


def _keys(*items: tuple[UUID, int, int]) -> tuple[SpanKey, ...]:
    return tuple(SpanKey(a, f"sha256:{a.hex}", s, e) for a, s, e in items)


def test_one_long_article_cannot_crowd_out_other_gold_articles() -> None:
    # Given: the top 6 chunks all belong to A; B and C come after.
    ranked = _keys(
        *((A, i * 1000, i * 1000 + 900) for i in range(6)), (B, 0, 900), (C, 0, 900)
    )

    chosen = select_spans(ranked, 2, None)

    assert [a for a, _ in chosen.groups] == [A, B, C]
    assert chosen.groups[0][1] == (0, 1)
    assert chosen.cap_dropped == 4


def test_overlapping_span_is_dropped_and_the_next_one_backfills() -> None:
    # Given: 2nd chunk shares 80% with the 1st; 3rd chunk is new text.
    ranked = _keys((A, 0, 1000), (A, 200, 1200), (A, 2000, 3000))

    chosen = select_spans(ranked, 2, 0.5)

    assert chosen.groups == ((A, (0, 2)),)
    assert chosen.overlap_dropped == 1


def test_overlap_without_any_candidate_left_just_keeps_fewer_spans() -> None:
    ranked = _keys((A, 0, 1000), (A, 100, 1000))

    chosen = select_spans(ranked, 3, 0.5)

    assert chosen.groups == ((A, (0,)),)
    assert chosen.overlap_dropped == 1


def test_single_article_question_keeps_several_distinct_passages() -> None:
    ranked = _keys((A, 0, 900), (A, 800, 1700), (A, 1600, 2500))  # 1-paragraph overlap

    chosen = select_spans(ranked, 3, 0.5)

    assert chosen.groups == ((A, (0, 1, 2)),)
    assert chosen.overlap_dropped == 0


def test_overlap_threshold_boundary_and_revision_scope() -> None:
    assert overlap_ratio((0, 100), (50, 150)) == 0.5
    at = select_spans(_keys((A, 0, 100), (A, 50, 150)), 3, 0.5)
    below = select_spans(_keys((A, 0, 100), (A, 51, 151)), 3, 0.5)
    other_revision = select_spans(
        (SpanKey(A, "sha256:old", 0, 100), SpanKey(A, "sha256:new", 0, 100)), 3, 0.5
    )

    assert at.overlap_dropped == 1  # >= threshold is dropped
    assert below.overlap_dropped == 0
    assert other_revision.overlap_dropped == 0  # never compared across revisions


def test_article_order_is_best_chunk_not_sum_of_chunks() -> None:
    # A has many mediocre chunks, B one best chunk: B ranks first.
    ranked = _keys((B, 0, 900), *((A, i * 1000, i * 1000 + 900) for i in range(5)))

    assert [a for a, _ in select_spans(ranked, 3, None).groups] == [B, A]


def test_cap_per_article_keeps_order() -> None:
    kept, dropped = cap_per_article([A, A, B, A, C, B], lambda a: a, 1)

    assert kept == (A, B, C)
    assert dropped == 3


@pytest.mark.parametrize(
    "build",
    [
        lambda: SelectionPolicy(max_spans_per_article=0),
        lambda: SelectionPolicy(overlap_threshold=0),
        lambda: SelectionPolicy(overlap_threshold=1.5),
        lambda: BranchDiversity(backfill_depth=100),  # backfill without a cap
        lambda: BranchDiversity(max_chunks_per_article=2, backfill_depth=500),
        lambda: BranchDiversity(max_chunks_per_article=2, max_backfill_calls=2),
        lambda: IntentFreshness(max_rank_shift=0),
        lambda: IntentFreshness(max_rank_shift=50),
        lambda: IntentFreshness(half_life_days=0),
        lambda: FieldBoosts(title=0),
    ],
)
def test_out_of_range_tuning_is_refused(build: object) -> None:
    with pytest.raises(InvalidTuningError):
        _ = build()  # pyright: ignore[reportCallIssue, reportUnknownVariableType]


# --- intent freshness ---------------------------------------------------------


def _fused(article: UUID, score: float, published: datetime | None) -> FusedCandidate:
    chunk = _chunk(article, 0)
    return FusedCandidate(
        chunk=chunk,
        fused_score=score,
        own_fused_score=score,
        lexical_rank=1,
        vector_rank=None,
        source_published_at=published,
        text="t",
        vector_generation=None,
    )


POLICY = IntentFreshness(mode="intent", half_life_days=7, max_rank_shift=5)
SHIFT = 1 / 61 - 1 / 66  # moving 5 ranks from the top in one branch


def _apply(
    items: tuple[FusedCandidate, ...], intent: str | None = "update"
) -> tuple[tuple[FusedCandidate, ...], FreshnessDecision]:
    return apply_intent_freshness(
        items, NOW, POLICY, intent, K, explicit_window=False, weight_sum=2.0
    )


def test_update_intent_promotes_a_newer_near_tie() -> None:
    old = _fused(A, 2 / 61, NOW - timedelta(days=30))
    new = _fused(B, 2 / 63, NOW - timedelta(days=1))

    ordered, decision = _apply((old, new))

    assert decision.applied
    assert [c.chunk.article_id for c in ordered] == [B, A]


def test_recent_dates_barely_reorder_close_relevance() -> None:
    # Both new (1 h apart): relevance order between them is kept.
    first = _fused(A, 2 / 64, NOW - timedelta(hours=20))
    second = _fused(B, 2 / 65, NOW - timedelta(hours=4))

    ordered, _ = _apply((first, second))

    assert [c.chunk.article_id for c in ordered] == [A, B]


def test_bonus_is_on_the_rrf_rank_scale() -> None:
    assert math.isclose(freshness_bonus(NOW, NOW, POLICY, K), SHIFT)
    assert math.isclose(freshness_bonus(NOW, NOW, POLICY, K, 2.0), 2 * SHIFT)
    week = freshness_bonus(NOW - timedelta(days=7), NOW, POLICY, K)
    assert math.isclose(week, SHIFT / 2)


@pytest.mark.parametrize("published", [None, NOW + timedelta(days=2)])
def test_missing_or_future_timestamps_get_no_bonus(published: datetime | None) -> None:
    item = _fused(A, 1 / 61, published)
    other = _fused(B, 1 / 61, NOW - timedelta(days=1))

    ordered, decision = _apply((item, other))

    assert freshness_bonus(published, NOW, POLICY, K) == 0.0
    assert ordered[0].chunk.article_id == B
    assert decision.future_timestamps + decision.missing_timestamps == 1


@pytest.mark.parametrize(
    ("intent", "explicit", "reason"),
    [
        ("temporal", False, "intent_not_eligible"),
        ("factual", False, "intent_not_eligible"),
        (None, False, "intent_not_eligible"),
        ("update", True, "explicit_window"),
    ],
)
def test_freshness_only_for_update_intent_without_explicit_window(
    intent: str | None,
    explicit: bool,  # noqa: FBT001 - parametrized
    reason: str,
) -> None:
    items = (_fused(A, 1 / 61, NOW - timedelta(days=30)), _fused(B, 1 / 62, NOW))

    ordered, decision = apply_intent_freshness(
        items, NOW, POLICY, intent, K, explicit_window=explicit
    )

    assert not decision.applied
    assert decision.reason == reason
    assert ordered == items


def test_policy_mode_is_a_no_op() -> None:
    items = (_fused(A, 1 / 61, NOW - timedelta(days=30)), _fused(B, 1 / 62, NOW))

    ordered, decision = apply_intent_freshness(
        items, NOW, IntentFreshness(), "update", K, explicit_window=False
    )

    assert ordered == items
    assert decision.reason == "policy_mode"


# --- profiles -----------------------------------------------------------------


def test_bundled_baseline_is_the_frozen_v3_tuning() -> None:
    assert bundled_tuning() == BASELINE_TUNING
    assert bundled_tuning("baseline") == BASELINE_TUNING


def test_every_bundled_profile_is_valid_and_distinct_in_values_or_name() -> None:
    profiles = bundled_profiles()

    assert {"baseline", "div2", "div3", "w1-2", "fresh7", "boost5-2-1"} <= set(profiles)
    assert profiles["div2"].selection.max_spans_per_article == 2
    assert profiles["div2"].diversity.max_chunks_per_article == 2
    assert profiles["div2"].selection.overlap_threshold == 0.5  # inherited
    assert profiles["w1-2"].weights == FusionWeights(1, 2)
    assert profiles["fresh7"].freshness.mode == "intent"


def _document(profiles: str) -> str:
    return '{"schema_version": "retrieval-tuning.v1", "profiles": ' + profiles + "}"


def test_unknown_profile_and_unknown_keys_fail_loudly() -> None:
    with pytest.raises(InvalidTuningError):
        _ = bundled_tuning("nope")
    with pytest.raises(InvalidTuningError):
        _ = parse_tuning("x", {"weigths": [1, 1]})
    with pytest.raises(InvalidTuningError):
        _ = load_profiles(_document('{"baseline": {"weights": [1, 2]}}'))
    with pytest.raises(InvalidTuningError):
        _ = load_profiles(
            _document('{"baseline": {}, "a": {"extends": "b"}, "b": {"extends": "a"}}')
        )


def test_tuning_description_changes_with_every_knob() -> None:
    described = {
        name: str(tuning.describe()) for name, tuning in bundled_profiles().items()
    }

    assert len(set(described.values())) == len(described)
    assert str(uuid4()) not in described["baseline"]


def test_fill_pass_adds_deeper_spans_of_selected_articles_only_after_the_cap() -> None:
    ranked = _keys(
        *((A, i * 1000, i * 1000 + 900) for i in range(5)), (B, 0, 900), (A, 9000, 9900)
    )

    chosen = select_spans(ranked, 3, 0.5, fill_per_article=5)

    assert chosen.groups == ((A, (0, 1, 2, 3, 4)), (B, (5,)))
    assert chosen.fill == frozenset({3, 4})
    assert chosen.cap_dropped == 1  # the 6th A span stays out
    assert select_spans(ranked, 3, 0.5).fill == frozenset()


# --- max_rank_shift is a guarantee on the final ordering ----------------------
# Finding P2 (codex-review-20261029): the bonus was capped by the RRF gap
# between ranks 1 and 1+R; RRF gaps shrink down the list, so a tail candidate
# moved 11 positions with R=5. The guard must hold on the output order.

OLD = NOW - timedelta(days=365)


def _published_branch(
    article: UUID, branch: Branch, rank: int, published: datetime | None
) -> BranchCandidate:
    return BranchCandidate(
        chunk=_chunk(article, 0),
        branch=branch,
        rank=rank,
        score=1.0,
        source_published_at=published,
        text="t",
        vector_generation=None,
    )


def _ids(count: int) -> list[UUID]:
    return [UUID(int=index + 1) for index in range(count)]


def _promotions(
    before: tuple[FusedCandidate, ...], after: tuple[FusedCandidate, ...]
) -> dict[UUID, int]:
    # One chunk per article in these lists, so the article identifies it.
    original = {c.chunk.article_id: i for i, c in enumerate(before)}
    return {
        c.chunk.article_id: original[c.chunk.article_id] - i
        for i, c in enumerate(after)
    }


def _assert_guard(
    before: tuple[FusedCandidate, ...], after: tuple[FusedCandidate, ...], shift: int
) -> dict[UUID, int]:
    # Same candidate set, every promotion <= R.
    assert sorted(c.chunk.chunk_id for c in before) == sorted(
        c.chunk.chunk_id for c in after
    )
    assert len({c.chunk.article_id for c in before}) == len(before)
    moved = _promotions(before, after)
    assert max(moved.values()) <= shift, moved
    return moved


def _freshen(
    fused: tuple[FusedCandidate, ...],
    shift: int = 5,
    weight_sum: float = 1.0,
    intent: str | None = "update",
    *,
    explicit_window: bool = False,
) -> tuple[tuple[FusedCandidate, ...], FreshnessDecision]:
    policy = IntentFreshness(mode="intent", half_life_days=7, max_rank_shift=shift)
    return apply_intent_freshness(
        fused,
        NOW,
        policy,
        intent,
        K,
        explicit_window=explicit_window,
        weight_sum=weight_sum,
    )


def test_reviewer_repro_tail_candidate_moves_at_most_r_positions() -> None:
    # k=60, one branch, 20 old candidates at ranks 20..39, a new one at rank 40.
    ids = _ids(21)
    lexical = [
        _published_branch(a, Branch.LEXICAL, 20 + i, OLD)
        for i, a in enumerate(ids[:20])
    ]
    lexical.append(_published_branch(ids[20], Branch.LEXICAL, 40, NOW))
    fused = fuse(lexical, [], K)

    ordered, decision = _freshen(fused)

    moved = _assert_guard(fused, ordered, 5)
    assert moved[ids[20]] == 5  # bounded, not 11
    assert decision.max_promotion == 5


@pytest.mark.parametrize(
    ("position", "expected"), [(2, 2), (15, 5), (39, 5)]
)  # head / middle / tail (0-based position of the new candidate)
def test_new_candidate_at_head_middle_tail_is_bounded(
    position: int, expected: int
) -> None:
    ids = _ids(40)
    lexical = [
        _published_branch(a, Branch.LEXICAL, i + 1, NOW if i == position else OLD)
        for i, a in enumerate(ids)
    ]
    fused = fuse(lexical, [], K)

    ordered, _ = _freshen(fused)

    moved = _assert_guard(fused, ordered, 5)
    assert moved[ids[position]] == expected


def test_head_gap_wider_than_r_ranks_is_not_bridged_by_the_bonus() -> None:
    # The RRF-scale bonus is still bounded at the head: a gap of 19 branch
    # ranks is never closed on date alone (this bounds promotion; it does not
    # establish that the promoted chunk is on-topic).
    first = _fused(A, 2 / 61, OLD)
    far = _fused(B, 2 / 80, NOW)

    ordered, _ = _apply((first, far))

    assert [c.chunk.article_id for c in ordered] == [A, B]


def test_equal_scores_are_deterministic_and_input_order_free() -> None:
    ids = _ids(12)
    lexical = [
        _published_branch(a, Branch.LEXICAL, 1, NOW if i % 4 == 3 else OLD)
        for i, a in enumerate(ids)
    ]
    fused = fuse(lexical, [], K)  # all fused scores equal

    first, _ = _freshen(fused, shift=2)
    second, _ = _freshen(tuple(reversed(fused)), shift=2)

    assert first == second
    _ = _assert_guard(fused, first, 2)


def test_near_equal_scores_with_many_new_candidates_and_swaps() -> None:
    ids = _ids(30)
    lexical = [
        _published_branch(
            a, Branch.LEXICAL, i + 1, NOW - timedelta(days=i % 3) if i % 3 else OLD
        )
        for i, a in enumerate(ids)
    ]
    fused = fuse(lexical, [], K)

    ordered, decision = _freshen(fused, shift=3)

    moved = _assert_guard(fused, ordered, 3)
    assert sum(1 for v in moved.values() if v > 0) > 1  # several swaps happened
    assert decision.boosted == 30  # old ones get a vanishing, non-zero bonus
    assert decision.max_promotion == max(moved.values())


@pytest.mark.parametrize(
    "weights",
    [
        FusionWeights(1.0, 0.0),
        FusionWeights(0.0, 1.0),
        FusionWeights(1.0, 1.0),
        FusionWeights(1.0, 2.0),
    ],
)
@pytest.mark.parametrize("shift", [1, 5, 20])
def test_guard_holds_for_two_branches_and_every_weighting(
    weights: FusionWeights, shift: int
) -> None:
    # Branches contribute differently: some chunks only lexical, some only
    # semantic (the other branch is missing for them), some both.
    ids = _ids(45)
    lexical = [
        _published_branch(a, Branch.LEXICAL, i + 1, NOW if i % 5 == 4 else OLD)
        for i, a in enumerate(ids[:30])
    ]
    vector = [
        _published_branch(a, Branch.VECTOR, i + 1, NOW if i % 7 == 6 else OLD)
        for i, a in enumerate(reversed(ids[15:]))
    ]
    fused = fuse(lexical, vector, K, weights)
    weight_sum = (weights.lexical if weights.lexical_enabled else 0.0) + (
        weights.semantic if weights.semantic_enabled else 0.0
    )

    ordered, decision = _freshen(fused, shift=shift, weight_sum=weight_sum)

    _ = _assert_guard(fused, ordered, shift)
    assert decision.max_promotion <= shift


def test_guard_holds_on_random_lists() -> None:
    rng = Random(20261029)  # noqa: S311 - seeded test data, not crypto
    for _ in range(300):
        count = rng.randint(1, 60)
        shift = rng.randint(1, 20)
        ids = _ids(count)
        lexical = [
            _published_branch(
                a,
                Branch.LEXICAL,
                rng.randint(1, 80),
                rng.choice(
                    [
                        None,
                        OLD,
                        NOW,
                        NOW - timedelta(hours=rng.randint(1, 900)),
                        NOW + timedelta(days=1),
                    ]
                ),
            )
            for a in ids
            if rng.random() < 0.8
        ]
        vector = [
            _published_branch(a, Branch.VECTOR, rng.randint(1, 80), None)
            for a in ids
            if rng.random() < 0.5
        ]
        fused = fuse(lexical, vector, K, FusionWeights(1.0, rng.choice([1.0, 2.0])))
        if not fused:
            continue
        ordered, _ = _freshen(fused, shift=shift, weight_sum=3.0)
        _ = _assert_guard(fused, ordered, shift)


@pytest.mark.parametrize(
    ("intent", "explicit"), [("factual", False), (None, False), ("update", True)]
)
def test_ineligible_requests_keep_the_baseline_order_exactly(
    intent: str | None,
    explicit: bool,  # noqa: FBT001 - parametrized
) -> None:
    ids = _ids(25)
    lexical = [
        _published_branch(a, Branch.LEXICAL, i + 1, NOW if i > 15 else OLD)
        for i, a in enumerate(ids)
    ]
    fused = fuse(lexical, [], K)

    ordered, decision = _freshen(fused, intent=intent, explicit_window=explicit)

    assert ordered == fused
    assert decision.max_promotion == 0


@pytest.mark.parametrize("published", [None, NOW + timedelta(days=3)])
def test_missing_or_future_timestamps_are_never_promoted(
    published: datetime | None,
) -> None:
    ids = _ids(25)
    lexical = [
        _published_branch(a, Branch.LEXICAL, i + 1, published if i == 24 else OLD)
        for i, a in enumerate(ids)
    ]
    fused = fuse(lexical, [], K)

    ordered, decision = _freshen(fused)

    assert _promotions(fused, ordered)[ids[24]] <= 0
    assert decision.boosted == 24  # the 24 old ones get a tiny bonus, not it


@pytest.mark.parametrize("duplicates", [False, True])
def test_guard_composed_like_the_product_dedup_first(duplicates: bool) -> None:  # noqa: FBT001
    # Reviewer setup (k=60, ranks 20..39 old, rank 40 new, distinct texts), in
    # the order RetrieveEvidence now uses: dedup, then freshness + guard. With
    # ``duplicates`` two old chunks share exact text (one representative).
    ids = _ids(21)
    items: list[FusedCandidate] = []
    for i, article_id in enumerate(ids):
        rank = 20 + i
        published = NOW if rank == 40 else OLD
        text = " ".join(f"marker{rank}{c}" for c in "abcde")
        if duplicates and rank == 26:
            text = " ".join(f"marker25{c}" for c in "abcde")
        items.append(replace(_fused(article_id, 1 / (K + rank), published), text=text))
    deduped, dropped = suppress_duplicates(
        tuple(items), bundled_retrieval_policy().dedup
    )

    ordered, decision = _freshen(deduped)

    assert dropped == (1 if duplicates else 0)
    moved = _assert_guard(deduped, ordered, 5)
    assert moved[ids[20]] == 5
    assert decision.max_promotion == max(moved.values())
