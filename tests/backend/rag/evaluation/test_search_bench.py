import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from backend.rag.application.ports.reranker import (
    RerankerIdentity,
    RerankOutput,
    RerankRequest,
)
from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.use_cases.persist import AccessDeniedError
from backend.rag.application.use_cases.retrieve import bundled_retrieval_policy
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument, normalized_content_hash
from backend.rag.domain.chunking import DEFAULT_POLICY
from backend.rag.domain.handoff import HandoffArticle
from backend.rag.domain.retrieval import ChunkRef
from backend.rag.domain.search_index import (
    EmbedderIdentity,
    IndexedChunk,
    InvalidEmbeddingError,
    ScoredChunk,
    checked_vector,
)
from backend.rag.evaluation.search_bench.bench import (
    BenchInputs,
    Engines,
    build_chunks,
    run_search_selection,
)
from backend.rag.evaluation.search_bench.candidates import (
    CandidateError,
    assign_splits,
    build_question_set,
    corpus_digest,
)
from backend.rag.evaluation.search_bench.labels import (
    GoldArticle,
    GoldSpan,
    QuestionFilters,
    QuestionSet,
    SearchQuestion,
    excerpt_digest,
)
from backend.rag.evaluation.search_bench.metrics import (
    auroc,
    ndcg_at,
    paired_bootstrap,
    percentile,
    recall_at,
    reciprocal_rank,
    span_hit_at,
)
from backend.rag.evaluation.search_bench.render import render
from backend.rag.evaluation.search_bench.runner import (
    ArticleFacts,
    run_benchmark,
    run_system,
)
from backend.rag.evaluation.search_bench.systems import (
    RerankSystem,
    RrfSystem,
    cap,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.hashing_embedder import HashingEmbedder
from backend.rag.infra.retrieval.memory_retrieval import InMemoryRetrievalStore
from backend.rag.infra.search.memory_bm25 import MemoryBm25Index, in_scope

CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
NOW = datetime(2026, 10, 26, tzinfo=UTC)
A, B, C = (UUID(f"00000000-0000-4000-8000-00000000000{i}") for i in (1, 2, 3))
HASH = normalized_content_hash("x")


def ref(article: UUID, index: int = 0, start: int = 0, end: int = 100) -> ChunkRef:
    return ChunkRef(article, HASH, DEFAULT_POLICY.version, index, start, end)


def hit(article: UUID, score: float, index: int = 0) -> ScoredChunk:
    return ScoredChunk(
        ref(article, index, index * 1000, index * 1000 + 1200), score, None
    )


def question(
    gold: Sequence[tuple[UUID, int]] = ((A, 2),),
    span: tuple[int, int] = (10, 20),
    qid: str = "q1",
    **overrides: object,
) -> SearchQuestion:
    data: dict[str, object] = {
        "qid": qid,
        "question": "giá vàng hôm nay",
        "category": "proper_noun",
        "split": "test",
        "answerable": bool(gold),
        "filters": QuestionFilters(),
        "gold": tuple(
            GoldArticle(
                article_id=article,
                content_hash=HASH,
                grade=2 if grade == 2 else 1,
                spans=(GoldSpan(start=span[0], end=span[1], excerpt_sha256="0" * 64),)
                if grade == 2
                else (),
            )
            for article, grade in gold
        ),
        "reference_answer": None,
        "label_status": "candidate",
        "origin": "machine_candidate",
    }
    data.update(overrides)
    return SearchQuestion.model_validate(data)


# --- metrics --------------------------------------------------------------


def test_recall_mrr_and_graded_ndcg() -> None:
    q = question(gold=((A, 2), (B, 1)))
    ranking = [C, B, A]

    assert recall_at(q, ranking, 1) == 0.0
    assert recall_at(q, ranking, 2) == 0.5
    assert recall_at(q, ranking, 3) == 1.0
    assert reciprocal_rank(q, ranking) == 0.5
    dcg = 1 / math.log2(3) + 3 / math.log2(4)
    idcg = 3 / math.log2(2) + 1 / math.log2(3)
    assert math.isclose(ndcg_at(q, ranking, 10), dcg / idcg)
    assert reciprocal_rank(q, [C]) == 0.0


def test_span_hit_needs_overlap_with_grade_two_span_of_same_revision() -> None:
    q = question(span=(1300, 1400))
    first = ScoredChunk(ref(A, 0, 0, 1200), 1.0, None)
    second = ScoredChunk(ref(A, 1, 1000, 2200), 0.9, None)
    other_hash = ScoredChunk(
        ChunkRef(
            A, normalized_content_hash("y"), DEFAULT_POLICY.version, 1, 1000, 2200
        ),
        1.0,
        None,
    )

    assert not span_hit_at(q, [first], 5)
    assert span_hit_at(q, [first, second], 5)
    assert not span_hit_at(q, [first, second], 1)
    assert not span_hit_at(q, [other_hash], 5)


def test_auroc_percentile_and_seeded_bootstrap() -> None:
    assert auroc([0.9, 0.8], [0.1]) == 1.0
    assert auroc([0.5], [0.5]) == 0.5
    assert auroc([], [0.1]) is None
    assert percentile([5.0, 1.0, 3.0, 2.0], 50) == 2.0
    assert percentile([], 95) is None
    a, b = [1.0, 1.0, 0.0, 1.0], [0.0, 1.0, 0.0, 0.0]
    first = paired_bootstrap(a, b, seed=7, resamples=500)
    again = paired_bootstrap(a, b, seed=7, resamples=500)
    assert first == again
    assert first.mean == 0.5
    assert (first.wins, first.losses, first.ties) == (2, 0, 2)


# --- labels / candidates --------------------------------------------------


def handoff(content: str, article_id: UUID = A) -> HandoffArticle:
    return HandoffArticle.model_validate(
        {
            "article_id": str(article_id),
            "article_version_id": str(uuid4()),
            "canonical_url": "https://example.vn/a",
            "title": "Tiêu đề",
            "content": content,
            "source_id": str(uuid4()),
            "source_name": "Nguồn",
            "source_domain": "example.vn",
            "published_at": "2026-10-20T08:00:00Z",
            "updated_at_source": None,
            "fetched_at": "2026-10-20T08:05:00Z",
            "language": "vi",
            "word_count": 3,
            "content_hash": "b" * 64,
            "normalization_version": "normalize-v4",
            "parse_version": "parser-v2",
            "quality_status": "passed",
            "quality_flags": [],
            "is_current": True,
        },
        strict=False,
    )


def candidate(
    quote: str, qid: str = "c1", category: str = "datetime"
) -> dict[str, object]:
    return {
        "candidate_id": qid,
        "question": "Khi nào?",
        "category": category,
        "filters": {},
        "reference_answer": "x",
        "gold": [{"article_id": str(A), "grade": 2, "quotes": [quote]}],
    }


def test_candidate_quote_becomes_offsets_and_digest() -> None:
    text = "Mở đầu. Phà dừng lúc 09:00. Kết thúc."
    built = build_question_set(
        [candidate("Phà dừng lúc 09:00.")],
        [handoff(text)],
        export_id="e1",
        chunker_version="c.v1",
        seed="s",
        dev_fraction=0.5,
    )

    span = built.questions[0].gold[0].spans[0]
    assert text[span.start : span.end] == "Phà dừng lúc 09:00."
    assert span.excerpt_sha256 == excerpt_digest("Phà dừng lúc 09:00.")
    assert built.questions[0].label_status == "candidate"
    assert built.corpus_digest == corpus_digest([(A, normalized_content_hash(text))])


@pytest.mark.parametrize(
    ("content", "quote", "message"),
    [
        ("Một câu.", "Không có", "not found"),
        ("Lặp. Lặp.", "Lặp.", "more than once"),
    ],
)
def test_missing_or_ambiguous_quotes_fail_the_build(
    content: str, quote: str, message: str
) -> None:
    with pytest.raises(CandidateError, match=message):
        _ = build_question_set(
            [candidate(quote)],
            [handoff(content)],
            export_id="e",
            chunker_version="c",
            seed="s",
            dev_fraction=0.5,
        )


def test_gold_outside_question_filters_fails_the_build() -> None:
    raw = candidate("Phà dừng lúc 09:00.")
    raw["filters"] = {"source_domains": ["other.vn"]}

    with pytest.raises(CandidateError, match="outside filters"):
        _ = build_question_set(
            [raw],
            [handoff("Mở đầu. Phà dừng lúc 09:00. Kết thúc.")],
            export_id="e",
            chunker_version="c",
            seed="s",
            dev_fraction=0.5,
        )


def test_splits_are_stratified_and_deterministic() -> None:
    rows = [
        candidate("q", f"c{i}", "datetime" if i % 2 else "paraphrase")
        for i in range(10)
    ]

    first = assign_splits(rows, "seed", 0.4)
    again = assign_splits(list(reversed(rows)), "seed", 0.4)

    assert first == again
    for category in ("datetime", "paraphrase"):
        members = [r["candidate_id"] for r in rows if r["category"] == category]
        assert sum(first[str(m)] == "dev" for m in members) == 2
    assert assign_splits(rows, "other", 0.4) != first


def test_no_answer_and_reviewed_label_rules() -> None:
    with pytest.raises(ValueError, match="no-answer"):
        _ = question(answerable=False)
    with pytest.raises(ValueError, match="reviewer"):
        _ = question(label_status="reviewed_accepted")
    assert question(gold=(), category="no_answer").answerable is False


# --- engines / systems ----------------------------------------------------


def document(article: UUID, text: str, domain: str = "example.vn") -> ArticleDocument:
    digest = normalized_content_hash(text)
    return ArticleDocument.model_validate(
        {
            "schema_version": "article_document.v1",
            "article_id": article,
            "source_id": UUID("00000000-0000-4000-8000-000000000012"),
            "title": "Bản tin",
            "raw_content": text,
            "normalization_version": "nfc.v1",
            "normalized_content": text,
            "canonical_url": "https://example.vn/x",
            "source_name": "Nguồn",
            "source_domain": domain,
            "source_published_at": datetime(2026, 10, 20, tzinfo=UTC),
            "ingested_at": NOW,
            "processed_at": NOW,
            "content_hash": digest,
            "content_fingerprint": digest,
            "visibility": "internal",
            "evidence_spans": (),
        }
    )


class MemoryVectors:
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[IndexedChunk, tuple[float, ...]]]] = {}

    def rebuild(
        self,
        identity: EmbedderIdentity,
        chunks: Sequence[IndexedChunk],
        vectors: Sequence[tuple[float, ...]],
    ) -> None:
        self.rows[identity.slug] = list(zip(chunks, vectors, strict=True))

    def search(
        self,
        identity: EmbedderIdentity,
        vector: tuple[float, ...],
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        scored = [
            ScoredChunk(
                item.chunk, sum(x * y for x, y in zip(v, vector, strict=True)), None
            )
            for item, v in self.rows[identity.slug]
            if in_scope(item, scope)
        ]
        scored.sort(
            key=lambda h: (-h.score, str(h.chunk.article_id), h.chunk.chunk_index)
        )
        return tuple(scored[:limit])

    def size_bytes(self, identity: EmbedderIdentity) -> int:
        return len(self.rows.get(identity.slug, ()))


class ReverseReranker:
    """Scores candidates by reverse input order (deterministic)."""

    broken: bool
    calls: int

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.calls = 0

    @property
    def identity(self) -> RerankerIdentity:
        return RerankerIdentity("reverse", "v1")

    def rerank(self, request: RerankRequest) -> RerankOutput:
        self.calls += 1
        if self.broken:
            return RerankOutput(scores=((uuid4(), 1.0),))
        n = len(request.candidates)
        return RerankOutput(
            scores=tuple(
                (c.candidate_id, float(i)) for i, c in enumerate(request.candidates)
            )
            if n
            else ()
        )


def test_checked_vector_never_pads_or_truncates() -> None:
    assert checked_vector((0.1, 0.2), 2) == (0.1, 0.2)
    with pytest.raises(InvalidEmbeddingError):
        _ = checked_vector((0.1,), 2)
    with pytest.raises(InvalidEmbeddingError):
        _ = checked_vector((0.1, math.nan), 2)


def test_cap_applies_per_article_limit_depth_and_ties() -> None:
    hits = [hit(A, 1.0, i) for i in range(5)] + [hit(B, 1.0, 0), hit(C, 0.5, 0)]

    capped = cap(hits, depth=4, per_article=2)

    assert [h.chunk.article_id for h in capped] == [A, A, B, C]


def test_memory_bm25_scope_filters_before_ranking() -> None:
    docs = [document(A, "giá vàng tăng mạnh"), document(B, "giá vàng giảm", "other.vn")]
    index = MemoryBm25Index()
    index.rebuild(build_chunks(docs, DEFAULT_POLICY))

    everything = index.search("giá vàng", RetrievalScope((), None, None, None), 10)
    scoped = index.search(
        "giá vàng", RetrievalScope(("other.vn",), None, None, None), 10
    )
    future = index.search(
        "giá vàng",
        RetrievalScope((), datetime(2026, 11, 1, tzinfo=UTC), None, None),
        10,
    )

    assert {h.chunk.article_id for h in everything} == {A, B}
    assert [h.chunk.article_id for h in scoped] == [B]
    assert future == ()


class _Fixed:
    _name: str
    hits: tuple[ScoredChunk, ...]

    def __init__(self, name: str, hits: tuple[ScoredChunk, ...]) -> None:
        self._name = name
        self.hits = hits

    @property
    def name(self) -> str:
        return self._name

    def describe(self) -> dict[str, object]:
        return {}

    def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
        _ = (query, scope)
        return self.hits


def test_rrf_uses_production_fusion_and_missing_branch_scores_zero() -> None:
    left = _Fixed("l", (hit(A, 9.0), hit(B, 8.0)))
    right = _Fixed("r", (hit(B, 0.9), hit(C, 0.8)))

    fused = RrfSystem(left, right, name="d").search(
        "q", RetrievalScope((), None, None, None)
    )

    assert [h.chunk.article_id for h in fused] == [B, A, C]
    assert math.isclose(fused[0].score, 1 / 62 + 1 / 61)
    assert math.isclose(fused[1].score, 1 / 61)


def test_rerank_reorders_window_and_invalid_output_keeps_fused_order() -> None:
    base = _Fixed("b", (hit(A, 3.0), hit(B, 2.0), hit(C, 1.0)))

    def texts(ids: Sequence[UUID]) -> dict[UUID, str]:
        return dict.fromkeys(ids, "t")

    good = RerankSystem(base, ReverseReranker(), texts, name="e", window=2)
    bad = RerankSystem(base, ReverseReranker(broken=True), texts, name="e", window=2)
    scope = RetrievalScope((), None, None, None)

    assert [h.chunk.article_id for h in good.search("q", scope)] == [B, A, C]
    assert [h.chunk.article_id for h in bad.search("q", scope)] == [A, B, C]


def test_runner_records_errors_and_nondeterminism() -> None:
    class Flaky:
        name: str = "flaky"
        calls: int = 0

        def describe(self) -> dict[str, object]:
            return {}

        def search(self, query: str, scope: RetrievalScope) -> tuple[ScoredChunk, ...]:
            _ = scope
            if query == "boom":
                raise InvalidEmbeddingError(1, 0)
            self.calls += 1
            return (hit(A, 1.0), hit(B, 1.0)) if self.calls % 2 else (hit(B, 1.0),)

    questions = [question(qid="ok"), question(qid="bad", question="boom")]
    ticks = iter(range(1000))

    run = run_system(Flaky(), questions, repeats=2, clock=lambda: float(next(ticks)))

    assert run.questions["bad"].error == "embedding_invalid"
    assert run.questions["bad"].latencies_ms == []
    assert not run.questions["ok"].deterministic
    assert run.questions["ok"].latencies_ms == [1000.0, 1000.0]


def test_access_is_checked_before_any_system_runs() -> None:
    q_set = QuestionSet(
        schema_version="search-questions.v1",
        corpus_export_id="e",
        corpus_digest=HASH,
        chunker_version="c",
        split_seed="s",
        dev_fraction=0.5,
        questions=(question(),),
    )
    system = _Fixed("x", ())

    with pytest.raises(AccessDeniedError):
        _ = run_benchmark(
            [system], q_set, FixtureAccessPolicy(revoked=True), CALLER, repeats=1
        )


def test_full_selection_run_with_fake_engines_is_text_free() -> None:
    texts = {
        A: "Giá vàng thế giới tăng hơn 1% khi đồng USD suy yếu trong phiên.",
        B: "Đội tuyển bóng đá giành chiến thắng đầu tiên tại giải.",
        C: "Lãi suất trái phiếu Mỹ kỳ hạn mười năm tăng mạnh.",
    }
    docs = tuple(document(article, text) for article, text in texts.items())
    store = InMemoryRetrievalStore()
    for item in docs:
        store.add(item)
    handoffs = [handoff(text, article) for article, text in texts.items()]
    raw: list[dict[str, object]] = [
        {
            "candidate_id": "c-gold",
            "question": "giá vàng USD",
            "category": "proper_noun",
            "filters": {},
            "reference_answer": "tăng",
            "gold": [{"article_id": str(A), "grade": 2, "quotes": ["tăng hơn 1%"]}],
        },
        {
            "candidate_id": "c-para",
            "question": "lãi suất trái phiếu",
            "category": "paraphrase",
            "filters": {},
            "reference_answer": "tăng",
            "gold": [{"article_id": str(C), "grade": 2, "quotes": ["tăng mạnh"]}],
        },
        {
            "candidate_id": "c-none",
            "question": "giá dầu thô",
            "category": "no_answer",
            "answerable": False,
            "filters": {},
            "reference_answer": None,
            "gold": [],
        },
    ]
    q_set = build_question_set(
        raw,
        handoffs,
        export_id="unit",
        chunker_version=DEFAULT_POLICY.version,
        seed="s",
        dev_fraction=0.5,
    )
    engines = Engines(
        store=store,
        lexical={"memory": MemoryBm25Index()},
        embedders={"hash16": HashingEmbedder(16)},
        vectors=MemoryVectors(),
        reranker=ReverseReranker(),
    )
    facts = {d.article_id: ArticleFacts(d.source_domain, "parser-v2", 60) for d in docs}

    report = run_search_selection(
        engines,
        BenchInputs(
            question_set=q_set,
            documents=docs,
            facts=facts,
            policy=bundled_retrieval_policy(),
            chunking=DEFAULT_POLICY,
            now=NOW,
            repeats=2,
            environment={"fake": True},
        ),
        FixtureAccessPolicy(),
        CALLER,
    )

    systems = cast("list[dict[str, object]]", report["systems"])
    names = [str(s["name"]) for s in systems]
    assert names[:3] == ["A_fts", "B_bm25[memory]", "C_sem[hash16]"]
    assert any(n.startswith("E_rerank[") for n in names)
    assert cast("dict[str, object]", report["run"])["selected_on_dev"] is not None
    assert cast("dict[str, object]", report["corpus"])["chunks"] == 3
    markdown = render(report)
    assert "A_fts" in markdown
    for text in texts.values():
        assert text not in str(report)
        assert text not in markdown
