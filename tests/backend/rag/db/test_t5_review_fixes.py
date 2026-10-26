"""Regressions for the T5 independent review (R1-R4, 2026-10-18).

R1 access re-checked after the final fetch and at the return boundary;
R2 any malformed reranker output degrades (cancel/deadline still stop);
R3 query lexemes come from the same PostgreSQL ``simple`` config as the index;
R4 lexical chunk scan and intermediate pool are bounded by frozen policy caps.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import override
from uuid import UUID

import pytest

from backend.rag.application.ports.reranker import RerankOutput, RerankRequest
from backend.rag.application.ports.retrieval import (
    CurrentArticleState,
    MetadataEligibility,
    RetrievalScope,
)
from backend.rag.application.use_cases.retrieve import RetrievalOptions
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.retrieval import (
    ChunkRef,
    EmptyReason,
    LexicalLimits,
    LexicalPage,
    QueryControl,
    RerankDegradeReason,
    RerankStatus,
    ResultStatus,
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalDeadlineExceededError,
    RetrievalResult,
    VectorHit,
)
from backend.rag.domain.tokenizer import TokenizerIdentity
from backend.rag.infra.llm.fixture_tokens import FixtureTokenCounter
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.retrieval.scripted_reranker import (
    ScriptedReranker,
    reverse_script,
)
from retrieval_support import (
    CALLER,
    POLICY,
    FakeMonotonic,
    MemoryStore,
    MutableAccess,
    article,
    briefing,
    control,
    service,
)

LEXICAL = RetrievalOptions(vector_enabled=False)
POLICIES = Path(__file__).parents[4] / "src/backend/rag/application/policies"


@dataclass(slots=True)
class BarrierStore:
    """Real Postgres store whose final fetch pauses on an Event barrier."""

    inner: PostgresRetrievalStore
    fetched: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    on_lexical: list[object] = field(default_factory=list[object])

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[str, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        return self.inner.metadata_eligibility(
            scope,
            topics,  # pyright: ignore[reportArgumentType]
            entity_ids,
            scan_cap,
        )

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        page = self.inner.lexical_chunks(query, scope, limits)
        for hook in self.on_lexical:
            hook()  # pyright: ignore[reportCallIssue]
        return page

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        return self.inner.chunk_texts(chunks)

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        return self.inner.vector_chunks(embedding, identity, scope, limit)

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        states = self.inner.current_articles(article_ids)  # real SQL read finished
        self.fetched.set()
        assert self.release.wait(timeout=30)
        return states


def persist(url: str, *texts: str) -> list[UUID]:
    store = PostgresRagStore(rag_engine(url))
    ids: list[UUID] = []
    for text in texts:
        document = article(text, title="Neutral title")
        store.persist_article(document)
        ids.append(document.article_id)
    return ids


# R1 ---------------------------------------------------------------------------


def test_r1_access_revoked_during_final_fetch_returns_no_evidence(
    retrieval_url: str,
) -> None:
    _ = persist(retrieval_url, "Harbor crane inspection closes the gate.")
    access = MutableAccess()
    store = BarrierStore(PostgresRetrievalStore(rag_engine(retrieval_url)))
    retrieve = service(store, access=access)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            retrieve.execute, briefing("harbor crane"), CALLER, control(), LEXICAL
        )
        assert store.fetched.wait(timeout=30)
        access.revoked = True  # revocation completes while the read is in flight
        store.release.set()
        with pytest.raises(RetrievalAccessDeniedError):
            _ = future.result(timeout=30)
    # lexical candidates + selected chunk texts + one batched final read
    assert store.inner.statements == 3


@dataclass(slots=True)
class RevokingCounter:
    """Token counter that revokes access during evidence assembly."""

    access: MutableAccess
    inner: FixtureTokenCounter = field(default_factory=FixtureTokenCounter)

    @property
    def identity(self) -> TokenizerIdentity:
        return self.inner.identity

    def count(self, text: str) -> int:
        self.access.revoked = True
        return self.inner.count(text)

    def prefix_end(self, text: str, max_tokens: int) -> int:
        return self.inner.prefix_end(text, max_tokens)


def test_r1_access_revoked_during_assembly_is_denied_at_return_boundary() -> None:
    store = MemoryStore()
    store.add(article("Harbor crane inspection closes the gate."))
    access = MutableAccess()
    retrieve = service(
        store, embeddings=None, access=access, reranker=None, use_default_reranker=False
    )
    retrieve = type(retrieve)(
        store=retrieve.store,
        embeddings=None,
        embedding_identity=retrieve.embedding_identity,
        reranker=None,
        tokens=RevokingCounter(access),
        access=access,
        policy=retrieve.policy,
        clock=retrieve.clock,
    )

    with pytest.raises(RetrievalAccessDeniedError):
        _ = retrieve.execute(briefing("harbor crane"), CALLER, control(), LEXICAL)


# R2 ---------------------------------------------------------------------------


class ExplodingFloat(float):
    @override
    def __float__(self) -> float:
        message = "hostile number"
        raise ValueError(message)


def three_element_row(request: RerankRequest) -> RerankOutput:
    first = request.candidates[0].candidate_id
    rows: list[tuple[object, ...]] = [(first, 1.0, 2.0)]
    rows.extend((c.candidate_id, 1.0) for c in request.candidates[1:])
    return RerankOutput(scores=tuple(rows))  # pyright: ignore[reportArgumentType]


def bare_id_rows(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple(c.candidate_id for c in request.candidates)  # pyright: ignore[reportArgumentType]
    )


def scores_not_a_sequence(request: RerankRequest) -> RerankOutput:
    del request
    return RerankOutput(scores=None)  # pyright: ignore[reportArgumentType]


def wrong_outer_type(request: RerankRequest) -> RerankOutput:
    return {"scores": reverse_script(request).scores}  # pyright: ignore[reportReturnType]


def huge_integer(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple((c.candidate_id, 10**400) for c in request.candidates)
    )


def hostile_float(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple((c.candidate_id, ExplodingFloat(1.0)) for c in request.candidates)
    )


def string_id(request: RerankRequest) -> RerankOutput:
    return RerankOutput(
        scores=tuple((str(c.candidate_id), 1.0) for c in request.candidates)
    )


@pytest.mark.parametrize(
    "script",
    [
        three_element_row,
        bare_id_rows,
        scores_not_a_sequence,
        wrong_outer_type,
        huge_integer,
        hostile_float,
        string_id,
    ],
)
def test_r2_malformed_rerank_output_degrades_to_fused_order(script: object) -> None:
    store = MemoryStore()
    for index in range(5):
        store.add(article(f"Harbor crane inspection bulletin {index} {'x' * index}."))
    fused = service(
        store, embeddings=None, reranker=None, use_default_reranker=False
    ).execute(briefing("harbor crane"), CALLER, control(), LEXICAL)

    result = service(
        store,
        embeddings=None,
        reranker=ScriptedReranker(script),  # pyright: ignore[reportArgumentType]
    ).execute(briefing("harbor crane"), CALLER, control(), LEXICAL)

    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.INVALID_OUTPUT
    assert [a.article_id for a in result.articles] == [
        a.article_id for a in fused.articles
    ]


def test_r2_malformed_output_on_real_db_degrades(retrieval_url: str) -> None:
    _ = persist(retrieval_url, "Harbor crane one.", "Harbor crane two again.")

    result = service(
        PostgresRetrievalStore(rag_engine(retrieval_url)),
        reranker=ScriptedReranker(three_element_row),
    ).execute(briefing("harbor crane"), CALLER, control(), LEXICAL)

    assert result.rerank_status is RerankStatus.DEGRADED
    assert result.rerank_degrade_reason is RerankDegradeReason.INVALID_OUTPUT
    assert result.status is ResultStatus.EVIDENCE


def test_r2_cancel_inside_malformed_rerank_still_stops_the_query() -> None:
    store = MemoryStore()
    store.add(article("Harbor crane inspection."))
    ctl = control()

    def cancel_then_malformed(request: RerankRequest) -> RerankOutput:
        ctl.cancel()
        return three_element_row(request)

    with pytest.raises(RetrievalCancelledError):
        _ = service(
            store, embeddings=None, reranker=ScriptedReranker(cancel_then_malformed)
        ).execute(briefing("harbor crane"), CALLER, ctl, LEXICAL)


# R3 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "query", "needle"),
    [
        ("Straße reopened today.", "Straße", "Straße"),
        ("Traffic on the East-Pier ramp resumed.", "east-pier", "East-Pier"),
        ("Gate closes on 2026-03-01 at noon.", "2026-03-01", "2026-03-01"),
        ("Cảng Hải Phòng ĐÓNG cửa hôm nay.", "đóng", "ĐÓNG"),
        ("İstanbul ferries resume.", "İstanbul", "İstanbul"),
        ("ΣΊΣΥΦΟΣ bridge closed.", "σίσυφοσ", "ΣΊΣΥΦΟΣ"),
        ("Café terrace closed.", "Café", "Café"),
        ("Contact ops@harbor.example for detours.", "ops@harbor.example", "ops@"),
    ],
)
def test_r3_query_lexemes_match_the_index_config(
    retrieval_url: str, content: str, query: str, needle: str
) -> None:
    (article_id,) = persist(retrieval_url, content)

    result = service(PostgresRetrievalStore(rag_engine(retrieval_url))).execute(
        briefing(query), CALLER, control(), LEXICAL
    )

    assert [a.article_id for a in result.articles] == [article_id]
    span = result.articles[0].spans[0].span
    assert needle in span.excerpt  # offsets are on the original NFC text
    assert content[span.start_offset : span.end_offset] == span.excerpt


def test_r3_stopword_only_query_reports_no_query_terms(retrieval_url: str) -> None:
    _ = persist(retrieval_url, "What is the harbor status?")

    result = service(PostgresRetrievalStore(rag_engine(retrieval_url))).execute(
        briefing("what is the"), CALLER, control(), LEXICAL
    )

    assert result.empty_reason is EmptyReason.NO_QUERY_TERMS


# R4 ---------------------------------------------------------------------------

STEP = 1000  # 1200-code-point chunks with 200 overlap


def long_text(chunks: int, *, needle_in_last: bool) -> str:
    """Exactly ``chunks`` T3 chunks; the query word only in the last one."""
    length = 1200 + (chunks - 1) * STEP
    body = ("lorem ipsum dolor sit amet " * (length // 27 + 1))[: length - 12]
    tail = " harborword." if needle_in_last else " plainword.."
    return body + tail


def test_r4_policy_freezes_scan_and_pool_caps() -> None:
    lexical = POLICY.lexical
    assert POLICY.policy_id == "retrieval-policy.v3"  # v2 superseded (R7)
    assert lexical.max_chunks_scanned_per_article == 40
    assert lexical.max_metadata_candidates == 2000
    assert lexical.max_text_chunks == 50
    assert lexical.depth == 50
    v1 = (POLICIES / "retrieval-policy.v1.json").read_bytes()
    assert b'"policy_id": "retrieval-policy.v1"' in v1  # history kept unchanged
    assert hashlib.sha256(v1).hexdigest()


@pytest.mark.parametrize(("chunks", "found"), [(40, True), (41, False)])
def test_r4_exact_and_one_over_scan_limit(
    retrieval_url: str,
    chunks: int,
    found: bool,  # noqa: FBT001
) -> None:
    (article_id,) = persist(retrieval_url, long_text(chunks, needle_in_last=True))

    result = service(PostgresRetrievalStore(rag_engine(retrieval_url))).execute(
        briefing("harborword"), CALLER, control(), LEXICAL
    )

    if found:
        assert [a.article_id for a in result.articles] == [article_id]
        assert result.metrics.lexical_truncated_articles == 0
    else:
        # Honest partial: FTS matched the article but the passage lies beyond
        # the frozen scan cap; reported, never silently widened.
        assert result.status is ResultStatus.EMPTY
        assert result.metrics.lexical_truncated_articles == 1


def lexical_rows(result: RetrievalResult) -> int:
    (stage,) = [s for s in result.metrics.stages if s.stage == "lexical"]
    return stage.candidates_in


def test_r4_long_article_intermediate_pool_is_bounded(retrieval_url: str) -> None:
    text = ("harborword crane inspection notice. " * 9000)[:312_000]
    _ = persist(retrieval_url, text, *(f"harborword item {i}" for i in range(60)))

    result = service(PostgresRetrievalStore(rag_engine(retrieval_url))).execute(
        briefing("harborword"), CALLER, control(), LEXICAL
    )

    assert lexical_rows(result) <= POLICY.lexical.max_metadata_candidates
    assert result.metrics.lexical_truncated_articles == 1
    long_spans = [a for a in result.articles if len(a.spans) > 1]
    assert all(
        len(a.spans) <= POLICY.lexical.max_chunks_per_article for a in long_spans
    )


def test_r4_cancel_and_deadline_after_long_scan_stop_the_query(
    retrieval_url: str,
) -> None:
    _ = persist(retrieval_url, long_text(45, needle_in_last=False) + " harborword")
    ctl = control()
    store = BarrierStore(PostgresRetrievalStore(rag_engine(retrieval_url)))
    store.release.set()
    store.on_lexical.append(ctl.cancel)

    with pytest.raises(RetrievalCancelledError):
        _ = service(store).execute(briefing("harborword"), CALLER, ctl)
    assert store.inner.statements == 1  # no vector/final read after the scan

    clock = FakeMonotonic()
    timed = QueryControl(deadline_seconds=1.0, monotonic=clock)
    late = BarrierStore(PostgresRetrievalStore(rag_engine(retrieval_url)))
    late.release.set()
    late.on_lexical.append(lambda: setattr(clock, "now", 5.0))
    with pytest.raises(RetrievalDeadlineExceededError):
        _ = service(late).execute(briefing("harborword"), CALLER, timed)
    assert late.inner.statements == 1
