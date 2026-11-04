"""Search-platform domain: RRF, identities, intents, hop bounds, outbox backoff."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.rag.domain.outbox import OutboxRetryPolicy
from backend.rag.domain.query_intent import (
    DEFAULT_HOP_POLICY,
    HopPolicy,
    HopStopReason,
    QueryIntent,
    classify_intent,
    expansion_query,
    second_hop_decision,
)
from backend.rag.domain.search_embedding import (
    BGE_M3,
    FAKE_BGE_M3,
    EmbeddingDimensionMismatchError,
    EmbeddingIdentityMismatchError,
    InvalidEmbeddingError,
    SearchEmbeddingIdentity,
    checked_vector,
)
from backend.rag.domain.search_index import (
    HybridSearchConfig,
    InvalidSearchConfigError,
    SearchBranch,
    SearchHit,
    SearchMode,
    revision_id_for,
    rrf_fuse,
    scopes_for_visibility,
    search_mode,
)

ARTICLE = UUID("22222222-2222-4222-8222-222222222222")


def _hit(chunk: UUID, branch: SearchBranch, rank: int) -> SearchHit:
    return SearchHit(
        chunk_id=chunk,
        article_id=ARTICLE,
        revision_id=revision_id_for(ARTICLE, "sha256:x"),
        content_hash="sha256:x",
        chunk_version="paragraph-900-1.v1",
        chunk_index=rank,
        char_start=0,
        char_end=1,
        text="t",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_domain="fixture.example",
        generation_id=1,
        branch=branch,
        rank=rank,
        score=100.0 / rank,
    )


def test_rrf_uses_ranks_not_raw_scores() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    lexical = (_hit(a, SearchBranch.LEXICAL, 1), _hit(b, SearchBranch.LEXICAL, 2))
    semantic = (_hit(b, SearchBranch.SEMANTIC, 1), _hit(c, SearchBranch.SEMANTIC, 2))
    fused = rrf_fuse(lexical, semantic, k=60, pool=10)
    assert [f.hit.chunk_id for f in fused] == [b, a, c]
    top = fused[0]
    assert (top.lexical_rank, top.semantic_rank, top.fused_rank) == (2, 1, 1)
    assert abs(top.fused_score - (1 / 62 + 1 / 61)) < 1e-12
    assert abs(top.lexical_contribution - 1 / 62) < 1e-12
    assert top.branches == (SearchBranch.LEXICAL, SearchBranch.SEMANTIC)


def test_rrf_pool_bounds_the_candidates_and_is_deterministic() -> None:
    lexical = tuple(_hit(uuid4(), SearchBranch.LEXICAL, r) for r in range(1, 51))
    semantic = tuple(_hit(uuid4(), SearchBranch.SEMANTIC, r) for r in range(1, 51))
    first = rrf_fuse(lexical, semantic, k=60, pool=45)
    assert len(first) == 45
    assert first == rrf_fuse(lexical, semantic, k=60, pool=45)


def test_hybrid_defaults_and_bounds() -> None:
    config = HybridSearchConfig()
    assert (config.bm25_depth, config.semantic_depth, config.rrf_k) == (50, 50, 60)
    assert 40 <= config.candidate_pool <= 50
    with pytest.raises(InvalidSearchConfigError):
        _ = HybridSearchConfig(bm25_depth=0)
    with pytest.raises(InvalidSearchConfigError):
        _ = HybridSearchConfig(candidate_pool=500)


def test_search_mode_labels() -> None:
    assert search_mode(3, 2) is SearchMode.HYBRID
    assert search_mode(3, 0) is SearchMode.LEXICAL_ONLY
    assert search_mode(0, 2) is SearchMode.SEMANTIC_ONLY
    assert search_mode(0, 0) is SearchMode.NONE


def test_unknown_visibility_maps_to_no_scope() -> None:
    assert scopes_for_visibility("internal") == ("fixture-internal",)
    assert scopes_for_visibility("public") == ()


def test_bge_m3_identity_is_1024_and_rejects_mismatches() -> None:
    assert (BGE_M3.identity, BGE_M3.dimensions) == ("bge-m3-1024.v1", 1024)
    with pytest.raises(EmbeddingIdentityMismatchError):
        BGE_M3.require(FAKE_BGE_M3)
    wrong_dims = SearchEmbeddingIdentity(BGE_M3.identity, BGE_M3.model, 768)
    with pytest.raises(EmbeddingDimensionMismatchError):
        BGE_M3.require(wrong_dims)


def test_checked_vector_rejects_wrong_dimension_and_invalid_values() -> None:
    with pytest.raises(EmbeddingDimensionMismatchError):
        _ = checked_vector((0.1,) * 1536, 1024)
    with pytest.raises(InvalidEmbeddingError):
        _ = checked_vector((0.0,) * 1024, 1024)
    with pytest.raises(InvalidEmbeddingError):
        _ = checked_vector((float("nan"),) + (0.1,) * 1023, 1024)


@pytest.mark.parametrize(
    ("query", "intent"),
    [
        ("Ai là chủ tịch Ủy ban Nhân dân Hà Nội?", QueryIntent.FACTUAL),
        ("Bao nhiêu người bị ảnh hưởng bởi bão Yagi?", QueryIntent.NUMERIC),
        ("Khi nào cầu Phong Châu được khởi công?", QueryIntent.TEMPORAL),
        ("Tiến độ mới nhất của dự án sân bay Long Thành?", QueryIntent.UPDATE),
        ("So sánh giá điện năm 2024 với năm 2025", QueryIntent.COMPARISON),
        ("Phản ứng của các bên về chính sách thuế mới", QueryIntent.MULTI_SOURCE),
        ("Giả sử lãi suất tăng thì sao?", QueryIntent.NO_ANSWER_CANDIDATE),
        ("Hà Nội", QueryIntent.NO_ANSWER_CANDIDATE),
        ("What is the latest update on the port closure?", QueryIntent.UPDATE),
        ("How many ships were delayed?", QueryIntent.NUMERIC),
    ],
)
def test_intent_rules(query: str, intent: QueryIntent) -> None:
    assert classify_intent(query).intent is intent


def test_intent_is_deterministic_and_versioned() -> None:
    first = classify_intent("Tiến độ mới nhất của dự án?")
    assert first == classify_intent("Tiến độ mới nhất của dự án?")
    assert first.classifier_version == "query-intent-rules.v1"
    assert first.matched_cue == "tiến độ"


ENABLED = HopPolicy(enabled=True)


def test_second_hop_is_disabled_by_default() -> None:
    assert not DEFAULT_HOP_POLICY.enabled
    decision = second_hop_decision(
        QueryIntent.UPDATE,
        primary_candidates=5,
        hops_done=1,
        expansions_done=0,
        remaining_seconds=30.0,
    )
    assert decision is HopStopReason.DISABLED


@pytest.mark.parametrize(
    ("intent", "candidates", "hops", "expansions", "remaining", "budget", "reason"),
    [
        (QueryIntent.UPDATE, 5, 1, 0, 30.0, 100, HopStopReason.ALLOWED),
        (QueryIntent.MULTI_SOURCE, 5, 1, 0, 30.0, None, HopStopReason.ALLOWED),
        (QueryIntent.FACTUAL, 5, 1, 0, 30.0, 100, HopStopReason.INTENT_NOT_ELIGIBLE),
        (QueryIntent.UPDATE, 0, 1, 0, 30.0, 100, HopStopReason.NO_PRIMARY_RESULTS),
        (QueryIntent.UPDATE, 5, 2, 0, 30.0, 100, HopStopReason.MAX_HOPS_REACHED),
        (QueryIntent.UPDATE, 5, 1, 1, 30.0, 100, HopStopReason.MAX_EXPANSIONS_REACHED),
        (QueryIntent.UPDATE, 5, 1, 0, 30.0, 0, HopStopReason.BUDGET_EXHAUSTED),
        (QueryIntent.UPDATE, 5, 1, 0, 2.0, 100, HopStopReason.DEADLINE_NEAR),
    ],
)
def test_second_hop_bounds(  # noqa: PLR0913, PLR0917
    intent: QueryIntent,
    candidates: int,
    hops: int,
    expansions: int,
    remaining: float,
    budget: int | None,
    reason: HopStopReason,
) -> None:
    decision = second_hop_decision(
        intent,
        primary_candidates=candidates,
        hops_done=hops,
        expansions_done=expansions,
        remaining_seconds=remaining,
        budget_remaining=budget,
        policy=ENABLED,
    )
    assert decision is reason


def test_expansion_query_is_bounded_and_deterministic() -> None:
    text = expansion_query(
        "tiến độ cầu Phong Châu", ("Cầu Phong Châu được thông xe ngày 15/3",), ENABLED
    )
    assert text.startswith("tiến độ cầu phong châu")
    assert "thông" in text
    assert len(text.split()) <= ENABLED.max_expansion_terms


def test_outbox_backoff_then_dead_letter() -> None:
    policy = OutboxRetryPolicy(max_attempts=4, base_seconds=5, factor=4, cap_seconds=60)
    assert [policy.delay_after(n) for n in (1, 2, 3)] == [5, 20, 60]
    assert policy.delay_after(4) is None
