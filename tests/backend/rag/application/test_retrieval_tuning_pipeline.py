"""RetrieveEvidence with opt-in tuning over the in-memory search index.

Covers the diversity path end to end (branch cap, bounded backfill,
selection, packing), zero-weight branches, intent freshness gating, and that
the PostgreSQL-side final check (revision, tombstone, scope) is unchanged.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import override
from uuid import UUID

from backend.rag.application.ports.candidates import CandidateBatch, CandidateSource
from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.retrieval_profiles import bundled_tuning
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from backend.rag.application.use_cases.search_candidates import (
    IndexedCandidateSource,
)
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import QueryControl, ResultStatus
from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    BranchDiversity,
    FusionWeights,
    IntentFreshness,
    RetrievalTuning,
    SelectionPolicy,
)
from backend.rag.domain.search_index import HybridSearchConfig
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from briefing_support import query
from retrieval_support import CALLER
from search_support import START, SearchHarness, article, search_harness

NOW = datetime(2026, 4, 1, tzinfo=UTC)
CROWD = bundled_tuning("div2")


def _paragraph(topic: str, index: int) -> str:
    filler = " ".join(f"chi tiết {index} phần {n} về {topic}" for n in range(28))
    return f"Đoạn {index}: cảng Đình Vũ {topic}. {filler}."


LONG = "\n\n".join(_paragraph("neo chờ tàu container", i) for i in range(8))
SHORT_B = "Hãng tàu B nói cảng Đình Vũ có 42 tàu container neo chờ ngoài phao số 0."
SHORT_C = "Cảng vụ C xác nhận tàu container neo chờ tại Đình Vũ được ưu tiên cập bến."


class CountingEmbedder(FakeBgeM3Embeddings):
    """Fake BGE-M3 that counts query embeddings."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: int = 0

    @override
    def embed_query(self, text: str) -> tuple[float, ...]:
        self.queries += 1
        return super().embed_query(text)


@dataclass(slots=True)
class World:
    store: InMemoryRetrievalStore = field(default_factory=InMemoryRetrievalStore)
    search: SearchHarness = field(
        default_factory=lambda: search_harness(embedder=CountingEmbedder())
    )

    def add(self, document: ArticleDocument) -> ArticleDocument:
        self.store.add(document)
        self.search.world.persist(document)
        return document

    def build(self) -> None:
        _ = self.search.rebuild.execute(self.search.embedder.identity)
        _ = self.search.drain.execute()

    @property
    def embedder(self) -> CountingEmbedder:
        embedder = self.search.embedder
        assert isinstance(embedder, CountingEmbedder)
        return embedder

    def retriever(
        self, tuning: RetrievalTuning = BASELINE_TUNING, depth: int = 4
    ) -> RetrieveEvidence:
        source = IndexedCandidateSource(
            index=self.search.index,
            embedder=self.search.embedder,
            generations=self.search.generations.active,
            config=HybridSearchConfig(bm25_depth=depth, semantic_depth=depth),
        )
        return RetrieveEvidence(
            store=self.store,
            embeddings=None,
            embedding_identity=EmbeddingIdentity(),
            reranker=None,
            tokens=FixtureTokenCounter(),
            access=FixtureAccessPolicy(),
            policy=bundled_retrieval_policy(),
            clock=lambda: NOW,
            candidates=source,
            fallback_to_store=False,
            tuning=tuning,
        )


def _crowded() -> tuple[World, ArticleDocument, ArticleDocument, ArticleDocument]:
    world = World()
    long = world.add(article(LONG, title="Cảng Đình Vũ: tàu container neo chờ"))
    b = world.add(article(SHORT_B, title="Hãng tàu B", domain="b.example"))
    c = world.add(article(SHORT_C, title="Cảng vụ C", domain="c.example"))
    world.build()
    return world, long, b, c


QUESTION = "tàu container neo chờ cảng Đình Vũ"
LEXICAL = RetrievalOptions(vector_enabled=False)


def _articles(world: World, tuning: RetrievalTuning, **overrides: object) -> list[UUID]:
    result = world.retriever(tuning).execute(
        query(QUESTION, **overrides), CALLER, QueryControl(10), LEXICAL
    )
    return [a.article_id for a in result.articles]


def test_one_long_article_crowds_the_branch_in_the_baseline() -> None:
    world, long, b, c = _crowded()

    found = _articles(world, BASELINE_TUNING)

    assert found[0] == long.article_id
    assert not {b.article_id, c.article_id} <= set(found)


def test_branch_cap_and_backfill_bring_the_other_sources_into_the_payload() -> None:
    world, long, b, c = _crowded()

    result = world.retriever(CROWD).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    assert {long.article_id, b.article_id, c.article_id} <= {
        a.article_id for a in result.articles
    }
    assert max(len(a.spans) for a in result.articles) <= 2
    assert result.metrics.branch_cap_dropped > 0
    assert result.metrics.backfill_calls == 1  # lexical branch only, bounded
    assert result.provenance.tuning_id == "div2"


def test_no_backfill_when_the_deadline_is_too_close() -> None:
    world, *_ = _crowded()
    tuning = replace(
        CROWD,
        diversity=BranchDiversity(
            max_chunks_per_article=2, backfill_depth=100, min_remaining_seconds=30.0
        ),
    )

    result = world.retriever(tuning).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    assert result.metrics.backfill_calls == 0
    assert result.metrics.branch_cap_dropped > 0


def test_no_backfill_when_the_branch_did_not_fill_its_depth() -> None:
    world, *_ = _crowded()

    result = world.retriever(CROWD, depth=50).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    assert result.metrics.backfill_calls == 0


def test_single_article_question_still_gets_several_passages() -> None:
    world = World()
    long = world.add(article(LONG, title="Cảng Đình Vũ: tàu container neo chờ"))
    world.build()

    result = world.retriever(bundled_tuning("div3")).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    assert [a.article_id for a in result.articles] == [long.article_id]
    assert len(result.articles[0].spans) == 3


def test_tombstone_revision_and_scope_checks_hold_with_tuning() -> None:
    world, long, b, c = _crowded()
    world.store.tombstone(b.article_id)  # index not drained: stale entry
    world.store.add(c.model_copy(update={"content_hash": "sha256:" + "0" * 64}))

    result = world.retriever(CROWD).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )
    scoped = world.retriever(CROWD).execute(
        query(QUESTION, source_domains=("fixture.example",)),
        CALLER,
        QueryControl(10),
        LEXICAL,
    )

    assert [a.article_id for a in result.articles] == [long.article_id]
    assert result.metrics.dropped_at_final_check >= 2
    assert {a.source_domain for a in scoped.articles} == {"fixture.example"}


def test_zero_semantic_weight_never_embeds_the_query() -> None:
    world, *_ = _crowded()
    lexical_only = replace(CROWD, weights=FusionWeights(1, 0))

    result = world.retriever(lexical_only).execute(
        query(QUESTION), CALLER, QueryControl(10)
    )

    assert result.status is ResultStatus.EVIDENCE
    assert world.embedder.queries == 0
    assert result.metrics.embedding_calls == 0
    assert all(s.vector_rank is None for a in result.articles for s in a.spans)


def test_zero_lexical_weight_only_returns_semantic_candidates() -> None:
    world, *_ = _crowded()
    semantic_only = replace(CROWD, weights=FusionWeights(0, 1))

    result = world.retriever(semantic_only).execute(
        query(QUESTION), CALLER, QueryControl(10)
    )

    assert result.status is ResultStatus.EVIDENCE
    assert all(s.lexical_rank is None for a in result.articles for s in a.spans)
    assert world.embedder.queries == 1


def test_breadth_first_packing_keeps_more_sources_under_a_tight_budget() -> None:
    world, long, b, c = _crowded()
    wide = RetrievalTuning(
        tuning_id="wide",
        diversity=BranchDiversity(max_chunks_per_article=3, backfill_depth=100),
    )
    budget = 700

    article_major = world.retriever(wide).execute(
        query(QUESTION, context_budget=budget), CALLER, QueryControl(10), LEXICAL
    )
    breadth = world.retriever(
        replace(wide, selection=SelectionPolicy(packing="breadth_first"))
    ).execute(query(QUESTION, context_budget=budget), CALLER, QueryControl(10), LEXICAL)

    assert len(breadth.articles) > len(article_major.articles)
    assert breadth.context.token_count <= budget
    # Offsets, hashes and excerpts are the original NFC slices.
    for item in breadth.articles:
        document = world.store.documents[item.article_id]
        for span in item.spans:
            assert span.span.content_hash == document.content_hash
            excerpt = document.normalized_content[
                span.span.start_offset : span.span.end_offset
            ]
            assert span.span.excerpt == excerpt
    assert {long.article_id, b.article_id, c.article_id} >= {
        a.article_id for a in breadth.articles
    }


def test_intent_freshness_runs_only_for_update_questions() -> None:
    world = World()
    old = world.add(article(SHORT_B, published=START))
    new = world.add(
        article(SHORT_C, published=NOW - timedelta(days=1), domain="c.example")
    )
    world.build()
    tuning = bundled_tuning("fresh7")
    retriever = world.retriever(tuning)

    update = retriever.execute(
        query("Tàu container neo chờ ở Đình Vũ mới nhất?"), CALLER, QueryControl(10)
    )
    factual = retriever.execute(
        query("Tàu container neo chờ ở Đình Vũ?"), CALLER, QueryControl(10)
    )
    window = retriever.execute(
        query("Tàu container neo chờ ở Đình Vũ mới nhất?", published_after=START),
        CALLER,
        QueryControl(10),
    )

    assert update.metrics.freshness == "applied"
    assert update.metrics.freshness_boosted >= 1
    assert (
        0 <= update.metrics.freshness_max_promotion <= tuning.freshness.max_rank_shift
    )
    assert factual.metrics.freshness_max_promotion == 0
    assert update.articles[0].article_id == new.article_id
    assert factual.metrics.freshness == "intent_not_eligible"
    assert window.metrics.freshness == "explicit_window"
    assert {a.article_id for a in factual.articles} == {old.article_id, new.article_id}


def test_baseline_tuning_matches_the_untuned_use_case() -> None:
    world, *_ = _crowded()
    tuned = world.retriever()
    default = RetrieveEvidence(
        store=tuned.store,
        embeddings=None,
        embedding_identity=tuned.embedding_identity,
        reranker=None,
        tokens=tuned.tokens,
        access=tuned.access,
        policy=tuned.policy,
        clock=tuned.clock,
        candidates=tuned.candidates,
        fallback_to_store=False,
    )

    left = default.execute(query(QUESTION), CALLER, QueryControl(10))
    right = tuned.execute(query(QUESTION), CALLER, QueryControl(10))

    assert left.articles == right.articles
    assert left.context == right.context


def test_fill_gives_a_deep_single_article_question_more_passages() -> None:
    world = World()
    long = world.add(article(LONG, title="Cảng Đình Vũ: tàu container neo chờ"))
    world.build()

    base = world.retriever(bundled_tuning("wrrf-selected"), depth=50).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )
    filled = world.retriever(bundled_tuning("fill6"), depth=50).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    assert [a.article_id for a in filled.articles] == [long.article_id]
    assert len(base.articles[0].spans) == 3
    assert len(filled.articles[0].spans) == 6
    assert filled.metrics.fill_spans_sent == 3
    starts = [s.span.start_offset for s in filled.articles[0].spans]
    assert starts == sorted(starts) or len(set(starts)) == 6
    assert filled.context.token_count <= filled.context.token_budget


def test_fill_never_displaces_a_base_span_or_another_source() -> None:
    world, long, b, c = _crowded()
    tight = 1_000

    fill6 = bundled_tuning("fill6")
    no_fill = replace(
        fill6, selection=replace(fill6.selection, fill_spans_per_article=None)
    )
    base = world.retriever(no_fill, depth=50).execute(
        query(QUESTION, context_budget=tight), CALLER, QueryControl(10), LEXICAL
    )
    filled = world.retriever(bundled_tuning("fill6"), depth=50).execute(
        query(QUESTION, context_budget=tight), CALLER, QueryControl(10), LEXICAL
    )
    roomy = world.retriever(bundled_tuning("fill6"), depth=50).execute(
        query(QUESTION), CALLER, QueryControl(10), LEXICAL
    )

    base_spans = {s.span.span_id for a in base.articles for s in a.spans}
    filled_spans = {s.span.span_id for a in filled.articles for s in a.spans}
    assert {a.article_id for a in filled.articles} >= {
        a.article_id for a in base.articles
    }
    assert base_spans <= filled_spans
    assert {long.article_id, b.article_id, c.article_id} <= {
        a.article_id for a in roomy.articles
    }
    assert roomy.metrics.fill_spans_sent > 0
    assert roomy.context.token_count <= roomy.context.token_budget


# --- max_rank_shift holds on the order RetrieveEvidence actually returns -------
# Follow-up review P2 (remediation-final/codex-review-20261029.md): dedup ran
# after the guard and re-sorted by boosted score, so the returned order moved a
# tail candidate 11 positions while the metric said 5 — even with no duplicate.

UPDATE_QUERY = "Tàu container neo chờ ở Đình Vũ mới nhất?"
GUARDED = replace(
    bundled_tuning("fresh7"),
    freshness=IntentFreshness(mode="intent", half_life_days=7, max_rank_shift=5),
)
UNBOOSTED = replace(GUARDED, freshness=replace(GUARDED.freshness, intents=()))


@dataclass(slots=True)
class RankedSource:
    """Real index candidates, lexical ranks overridden per article (one chunk each)."""

    inner: CandidateSource
    ranks: dict[UUID, int]

    def candidates(  # noqa: PLR0913 - CandidateSource signature
        self,
        text: str,
        scope: RetrievalScope,
        query: BriefingQuery,
        caller: CallerScope,
        control: QueryControl,
        *,
        lexical: bool,
        semantic: bool,
        tuning: RetrievalTuning | None = None,
    ) -> CandidateBatch:
        batch = self.inner.candidates(
            text,
            scope,
            query,
            caller,
            control,
            lexical=lexical,
            semantic=semantic,
            tuning=tuning,
        )
        ranked = tuple(
            replace(item, rank=self.ranks[item.chunk.article_id])
            for item in batch.lexical
            if item.chunk.article_id in self.ranks
        )
        assert len(ranked) == len(self.ranks)  # every article came back
        return replace(batch, lexical=ranked, vector=())


def _story(index: int, body: str | None = None) -> str:
    unique = " ".join(f"mục{index}x{n}" for n in range(30))
    return body or f"Tàu container neo chờ ở Đình Vũ, bản tin {index}: {unique}."


def _tail_world(*, duplicate_pair: bool = False) -> tuple[World, dict[UUID, int], UUID]:
    # 20 month-old articles at lexical ranks 20..39, one new article at rank 40.
    world = World()
    ranks: dict[UUID, int] = {}
    for index in range(20):
        body = _story(5) if duplicate_pair and index == 6 else None  # copy of #5
        doc = world.add(
            article(_story(index, body), domain=f"d{index}.example", published=START)
        )
        ranks[doc.article_id] = 20 + index
    new = world.add(
        article(_story(99), domain="new.example", published=NOW - timedelta(days=1))
    )
    ranks[new.article_id] = 40
    world.build()
    return world, ranks, new.article_id


def _ranked_ids(
    world: World, ranks: dict[UUID, int], tuning: RetrievalTuning
) -> tuple[list[UUID], RetrieveEvidence]:
    base = world.retriever(tuning, depth=60)
    assert base.candidates is not None
    retriever = replace(base, candidates=RankedSource(base.candidates, ranks))
    draft = retriever.fuse(
        retriever.retrieve_primary(
            retriever.apply_filters(
                query(UPDATE_QUERY, result_limit=20), CALLER, QueryControl(10), LEXICAL
            )
        )
    )
    assert draft.ranked is not None
    return [item.candidate.chunk.article_id for item in draft.ranked], retriever


def _promotion(before: list[UUID], after: list[UUID]) -> dict[UUID, int]:
    original = {a: i for i, a in enumerate(before)}
    return {a: original[a] - i for i, a in enumerate(after)}


def test_returned_order_respects_the_guard_without_duplicates() -> None:
    world, ranks, new = _tail_world()

    baseline, _ = _ranked_ids(world, ranks, UNBOOSTED)
    boosted, retriever = _ranked_ids(world, ranks, GUARDED)
    result = retriever.execute(
        query(UPDATE_QUERY, result_limit=20), CALLER, QueryControl(10), LEXICAL
    )

    moved = _promotion(baseline, boosted)
    assert sorted(baseline, key=str) == sorted(boosted, key=str)  # same set
    assert max(moved.values()) <= GUARDED.freshness.max_rank_shift
    assert moved[new] == GUARDED.freshness.max_rank_shift  # bounded, not 11
    assert result.metrics.suppressed_duplicates == 0
    assert result.metrics.freshness == "applied"
    assert result.metrics.freshness_max_promotion == max(moved.values())
    returned = [a.article_id for a in result.articles]
    assert returned == list(dict.fromkeys(boosted))[: len(returned)]


def test_returned_order_respects_the_guard_with_duplicate_representatives() -> None:
    world, ranks, new = _tail_world(duplicate_pair=True)

    baseline, plain = _ranked_ids(world, ranks, UNBOOSTED)
    boosted, retriever = _ranked_ids(world, ranks, GUARDED)
    fresh = retriever.execute(
        query(UPDATE_QUERY, result_limit=20), CALLER, QueryControl(10), LEXICAL
    )
    unboosted = plain.execute(
        query(UPDATE_QUERY, result_limit=20), CALLER, QueryControl(10), LEXICAL
    )

    assert len(baseline) == len(ranks) - 1  # one exact copy suppressed
    assert sorted(baseline, key=str) == sorted(boosted, key=str)
    moved = _promotion(baseline, boosted)
    assert max(moved.values()) <= GUARDED.freshness.max_rank_shift
    assert moved[new] == GUARDED.freshness.max_rank_shift
    assert fresh.metrics.suppressed_duplicates == 1
    assert unboosted.metrics.suppressed_duplicates == 1
    assert fresh.metrics.freshness_max_promotion == max(moved.values())
