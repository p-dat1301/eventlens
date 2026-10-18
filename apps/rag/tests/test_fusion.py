"""Pure D35/D43 ranking behavior: RRF, ties, freshness, dedup, rerank order."""

import itertools
import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError
from retrieval_support import (  # pyright: ignore[reportImplicitRelativeImport]
    NOW,
    POLICY,
    ROOT,
    article,
    fixture_article,
)

from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import DEFAULT_POLICY, chunk_bounds, chunk_document
from eventlens_rag.domain.fusion import (
    aggregate_articles,
    freshness_bonus,
    fuse,
    order_after_rerank,
    rank_lexical,
    rank_vector,
    suppress_duplicates,
    validate_rerank_scores,
)
from eventlens_rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    ChunkRef,
    FusedCandidate,
    LexicalChunk,
    LexicalPage,
    RetrievalPolicy,
    VectorHit,
)

JSON = TypeAdapter(dict[str, object])
POLICY_FILE = "apps/rag/src/eventlens_rag/policies/retrieval-policy.v3.json"


def ref(article: int, index: int = 0) -> ChunkRef:
    return ChunkRef(
        article_id=UUID(int=article),
        content_hash="sha256:" + "0" * 64,
        chunker_version=DEFAULT_POLICY.version,
        chunk_index=index,
        start_offset=index * 1000,
        end_offset=index * 1000 + 10,
    )


def candidate(
    chunk: ChunkRef,
    branch: Branch,
    rank: int,
    published: datetime | None = None,
    text: str = "distinct text",
) -> BranchCandidate:
    return BranchCandidate(
        chunk=chunk,
        branch=branch,
        rank=rank,
        score=1.0,
        source_published_at=published,
        text=f"{text} {chunk.article_id} {chunk.chunk_index}",
        vector_generation=7 if branch is Branch.VECTOR else None,
    )


def fused(
    article: int,
    score: float,
    published: datetime | None,
    text: str,
    index: int = 0,
) -> FusedCandidate:
    return FusedCandidate(
        chunk=ref(article, index),
        fused_score=score,
        own_fused_score=score,
        lexical_rank=1,
        vector_rank=None,
        source_published_at=published,
        text=text,
        vector_generation=None,
    )


def test_policy_freezes_d35_d43_constants() -> None:
    assert POLICY.policy_id == "retrieval-policy.v3"
    assert POLICY.supersedes == "retrieval-policy.v2"
    assert POLICY.fusion_policy_id == "rrf-k60.v1"
    assert (POLICY.lexical.depth, POLICY.vector.depth) == (50, 50)
    assert POLICY.rrf.k == 60
    assert POLICY.rrf.missing_branch_contribution == 0
    assert POLICY.rerank.max_candidates == 40
    assert POLICY.rerank.max_tokens_per_candidate == 512
    assert POLICY.rerank.timeout_seconds == 1.5
    assert POLICY.output.max_articles == 20
    assert POLICY.output.max_context_tokens == 12_000
    assert POLICY.vector.route == "exact"
    assert POLICY.vector.ann_enabled is False
    assert POLICY.chunker_version == DEFAULT_POLICY.version


def test_policy_rejects_changed_tie_break_or_relaxed_caps() -> None:
    raw = TypeAdapter(dict[str, object]).validate_json(
        (ROOT / POLICY_FILE).read_bytes()
    )
    sections = TypeAdapter(dict[str, dict[str, object]]).validate_python(
        {key: value for key, value in raw.items() if isinstance(value, dict)}
    )
    raw.update(sections)
    sections["rrf"]["ties"] = "fused_score_desc,article_id_asc"
    with pytest.raises(ValidationError, match="tie"):
        _ = RetrievalPolicy.model_validate_json(JSON.dump_json(raw))
    sections["rrf"]["ties"] = POLICY.rrf.ties
    sections["rerank"]["max_candidates"] = 41
    with pytest.raises(ValidationError):
        _ = RetrievalPolicy.model_validate_json(JSON.dump_json(raw))
    sections["rerank"]["max_candidates"] = 40
    sections["rrf"]["k"] = 61
    with pytest.raises(ValidationError):
        _ = RetrievalPolicy.model_validate_json(JSON.dump_json(raw))


def test_rrf_arithmetic_and_missing_branch_contributes_zero() -> None:
    a, b, c = ref(1), ref(2), ref(3)
    lexical = (candidate(a, Branch.LEXICAL, 1), candidate(b, Branch.LEXICAL, 2))
    vector = (candidate(b, Branch.VECTOR, 1), candidate(c, Branch.VECTOR, 3))

    result = {item.chunk.article_id.int: item for item in fuse(lexical, vector, 60)}

    assert result[1].fused_score == 1 / 61
    assert result[2].fused_score == 1 / 62 + 1 / 61
    assert result[3].fused_score == 1 / 63
    assert (result[1].lexical_rank, result[1].vector_rank) == (1, None)
    assert result[2].branches == (Branch.LEXICAL, Branch.VECTOR)
    assert result[3].vector_generation == 7
    assert [item.chunk.article_id.int for item in fuse(lexical, vector, 60)] == [
        2,
        1,
        3,
    ]


def test_duplicate_chunk_identity_in_one_branch_keeps_best_rank_once() -> None:
    a = ref(1)
    lexical = (candidate(a, Branch.LEXICAL, 3), candidate(a, Branch.LEXICAL, 1))

    result = fuse(lexical, (), 60)

    assert len(result) == 1
    assert result[0].fused_score == 1 / 61


def test_fused_ties_are_stable_under_shuffled_input() -> None:
    newer = datetime(2026, 3, 2, tzinfo=UTC)
    older = datetime(2026, 3, 1, tzinfo=UTC)
    # Same rank in different branches -> identical fused scores (1/61).
    items = [
        candidate(ref(4), Branch.LEXICAL, 1, older),
        candidate(ref(3), Branch.VECTOR, 1, newer),
        candidate(ref(2, 1), Branch.LEXICAL, 1, None),
        candidate(ref(2, 0), Branch.VECTOR, 1, None),
        candidate(ref(1), Branch.VECTOR, 1, older),
    ]
    orders: set[tuple[tuple[int, int], ...]] = set()
    for permutation in itertools.permutations(items):
        lexical = tuple(i for i in permutation if i.branch is Branch.LEXICAL)
        vector = tuple(i for i in permutation if i.branch is Branch.VECTOR)
        orders.add(
            tuple(
                (c.chunk.article_id.int, c.chunk.chunk_index)
                for c in fuse(lexical, vector, 60)
            )
        )

    assert len(orders) == 1
    order = orders.pop()
    # newest first, then older by article ID, then NULL publication last
    assert order[0] == (3, 0)
    assert order[1:3] == ((1, 0), (4, 0))
    nulls = order[3:]
    expected_nulls = sorted(((2, 0), (2, 1)), key=lambda pair: str(ref(*pair).chunk_id))
    assert list(nulls) == expected_nulls


def test_freshness_uses_fixed_clock_and_ignores_missing_or_future_times() -> None:
    policy = POLICY.freshness
    assert math.isclose(freshness_bonus(NOW, NOW, policy), 0.05)
    month_old = NOW - timedelta(days=30)
    assert math.isclose(freshness_bonus(month_old, NOW, policy), 0.025)
    assert freshness_bonus(None, NOW, policy) == 0.0
    assert freshness_bonus(NOW + timedelta(seconds=1), NOW, policy) == 0.0


def test_freshness_breaks_equal_relevance_inside_a_branch_only() -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 3, 30, tzinfo=UTC)
    hits = [
        VectorHit(UUID(int=1), "sha256:" + "1" * 64, 1, 0, 0, 5, "alpha", old, 0.2),
        VectorHit(UUID(int=2), "sha256:" + "2" * 64, 2, 0, 0, 5, "beta", new, 0.2),
    ]

    ranked = rank_vector(hits, NOW, POLICY.vector, POLICY.freshness, "c")

    assert [c.chunk.article_id.int for c in ranked] == [2, 1]
    assert [c.rank for c in ranked] == [1, 2]
    # RRF still uses only ranks: 1/61 and 1/62, independent of the bonus.
    scores = [c.fused_score for c in fuse((), ranked, 60)]
    assert scores == [1 / 61, 1 / 62]


def test_branch_depth_and_per_article_caps_are_enforced() -> None:
    hits = [
        VectorHit(
            UUID(int=article),
            "sha256:" + "3" * 64,
            article,
            index,
            index,
            index + 1,
            "x",
            None,
            0.1 + index / 100,
        )
        for article in range(1, 40)
        for index in range(5)
    ]

    ranked = rank_vector(hits, NOW, POLICY.vector, POLICY.freshness, "c")

    assert len(ranked) == 50
    assert [c.rank for c in ranked] == list(range(1, 51))
    per_article = [c.chunk.article_id for c in ranked]
    assert max(per_article.count(a) for a in set(per_article)) <= 3


def page_for(document: ArticleDocument, matched: int, terms: int) -> LexicalPage:
    return LexicalPage(
        term_count=terms,
        chunks=(
            LexicalChunk(
                article_id=document.article_id,
                content_hash=document.content_hash,
                source_published_at=document.source_published_at,
                chunk_index=0,
                start_offset=0,
                end_offset=len(document.normalized_content),
                matched_terms=matched,
                document_rank=0.5,
            ),
        ),
        truncated_articles=0,
    )


def test_lexical_branch_ranks_chunks_with_shared_identity() -> None:
    document = fixture_article(2)

    ranked = rank_lexical(
        page_for(document, 3, 3),
        NOW,
        POLICY.lexical,
        POLICY.freshness,
        DEFAULT_POLICY.version,
    )

    assert len(ranked) == 1
    assert ranked[0].chunk.chunk_index == 0
    bonus = freshness_bonus(document.source_published_at, NOW, POLICY.freshness)
    assert math.isclose(ranked[0].score, 1.0 + bonus)
    # Same deterministic identity a vector hit for the same chunk gets.
    vector = rank_vector(
        (
            VectorHit(
                document.article_id,
                document.content_hash,
                9,
                0,
                0,
                len(document.normalized_content),
                document.normalized_content,
                document.source_published_at,
                0.3,
            ),
        ),
        NOW,
        POLICY.vector,
        POLICY.freshness,
        DEFAULT_POLICY.version,
    )
    assert vector[0].chunk.chunk_id == ranked[0].chunk.chunk_id
    assert len(fuse(ranked, vector, 60)) == 1


def test_no_query_lexemes_or_no_match_yields_no_lexical_candidates() -> None:
    document = fixture_article(1)
    for page in (page_for(document, 0, 0), page_for(document, 0, 3)):
        assert (
            rank_lexical(
                page, NOW, POLICY.lexical, POLICY.freshness, DEFAULT_POLICY.version
            )
            == ()
        )


@pytest.mark.parametrize(
    "length", [1, 999, 1000, 1199, 1200, 1201, 2200, 2201, 312_000]
)
def test_chunk_bounds_arithmetic_equals_t3_chunker(length: int) -> None:
    document = article("x" * length)
    chunks = chunk_document(document)

    for chunk in chunks:
        assert chunk_bounds(length, chunk.index) == (
            chunk.start_offset,
            chunk.end_offset,
        )
    assert chunk_bounds(length, len(chunks)) is None
    assert chunk_bounds(length, -1) is None


def test_near_duplicate_wire_copy_keeps_earliest_original_as_representative() -> None:
    original = fixture_article(1)  # published 2026-03-01
    wire = fixture_article(22)  # near-duplicate wire copy, 2026-03-22
    conflict_a, conflict_b = fixture_article(3), fixture_article(4)
    items = [
        fused(22, 0.05, wire.source_published_at, wire.normalized_content),
        fused(3, 0.04, conflict_a.source_published_at, conflict_a.normalized_content),
        fused(1, 0.03, original.source_published_at, original.normalized_content),
        fused(4, 0.02, conflict_b.source_published_at, conflict_b.normalized_content),
    ]

    kept, suppressed = suppress_duplicates(items, POLICY.dedup)

    assert suppressed == 1
    assert [k.chunk.article_id.int for k in kept] == [1, 3, 4]
    representative = kept[0]
    assert representative.fused_score == 0.05  # cluster best score
    assert representative.own_fused_score == 0.03
    assert representative.duplicate_article_ids == (UUID(int=22),)
    # Conflicting (not duplicate) capacity reports are both kept.
    assert {k.chunk.article_id.int for k in kept} >= {3, 4}


def test_exact_duplicate_text_is_suppressed_even_within_one_article() -> None:
    text = "Same   text\nhere."
    items = [
        fused(5, 0.02, None, text, index=1),
        fused(5, 0.03, None, "same text here.", index=0),
    ]

    kept, suppressed = suppress_duplicates(items, POLICY.dedup)

    assert suppressed == 1
    assert len(kept) == 1
    assert kept[0].fused_score == 0.03


def test_same_article_near_duplicates_are_not_merged() -> None:
    base = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    items = [
        fused(6, 0.03, None, base, index=0),
        fused(6, 0.02, None, base + " lambda", index=1),
    ]

    kept, suppressed = suppress_duplicates(items, POLICY.dedup)

    assert (len(kept), suppressed) == (2, 0)


@pytest.mark.parametrize(
    "returned",
    [
        [(UUID(int=1), 1.0), (UUID(int=2), 2.0), (UUID(int=9), 0.0)],  # unknown
        [(UUID(int=1), 1.0), (UUID(int=1), 2.0)],  # duplicate (and missing)
        [(UUID(int=1), 1.0)],  # missing
        [(UUID(int=1), math.nan), (UUID(int=2), 1.0)],
        [(UUID(int=1), math.inf), (UUID(int=2), 1.0)],
        [(UUID(int=1), True), (UUID(int=2), 1.0)],
        [(UUID(int=1), "1.0"), (UUID(int=2), 1.0)],
        [("not-a-uuid", 1.0), (UUID(int=2), 1.0)],
    ],
)
def test_invalid_rerank_outputs_are_rejected_whole(
    returned: list[tuple[object, object]],
) -> None:
    assert validate_rerank_scores([UUID(int=1), UUID(int=2)], returned) is None


def test_valid_rerank_output_reorders_only_the_known_window_then_tail() -> None:
    window = [fused(1, 0.03, None, "a"), fused(2, 0.02, None, "b")]
    tail = [fused(3, 0.01, None, "c")]
    scores = validate_rerank_scores(
        [w.chunk.chunk_id for w in window],
        [(window[0].chunk.chunk_id, 0.1), (window[1].chunk.chunk_id, 0.9)],
    )

    ranked = order_after_rerank(window, tail, scores)

    assert [r.candidate.chunk.article_id.int for r in ranked] == [2, 1, 3]
    assert [r.position for r in ranked] == [1, 2, 3]
    assert ranked[2].rerank_score is None


def test_reranked_ties_fall_back_to_fused_order() -> None:
    window = [fused(2, 0.02, None, "b"), fused(1, 0.03, None, "a")]
    scores = {w.chunk.chunk_id: 0.5 for w in window}

    ranked = order_after_rerank(window, (), scores)

    assert [r.candidate.chunk.article_id.int for r in ranked] == [1, 2]


def test_article_aggregation_by_best_chunk_with_bounded_spans() -> None:
    window = [
        fused(2, 0.05, None, "b0", 0),
        fused(1, 0.04, None, "a0", 0),
        fused(2, 0.03, None, "b1", 1),
        fused(2, 0.02, None, "b2", 2),
        fused(2, 0.01, None, "b3", 3),
    ]
    ranked = order_after_rerank(window, (), None)

    groups = aggregate_articles(ranked, 3)

    assert [g[0].int for g in groups] == [2, 1]
    assert [r.candidate.chunk.chunk_index for r in groups[0][1]] == [0, 1, 2]
