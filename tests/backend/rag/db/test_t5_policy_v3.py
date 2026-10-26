"""Re-review 2 (R7): retrieval-policy.v3 caps are real runtime bounds.

* every numeric v3 cap is wired: changing it changes observed runtime behavior;
* real-DB worst cases: 50 x 40 tied windows = exactly 2,000 metadata rows,
  the 51st article excluded deterministically, the 41st window not scanned and
  reported, at most 50 text rows, constant statement count.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel

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
    LexicalLimits,
    LexicalPage,
    RetrievalConfigurationError,
    RetrievalPolicy,
    RetrievalResult,
    VectorHit,
)
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.retrieval.scripted_reranker import ScriptedReranker
from retrieval_support import (
    CALLER,
    POLICY,
    MemoryStore,
    article,
    briefing,
    control,
    metadata_for,
    service,
)

LEXICAL = RetrievalOptions(vector_enabled=False, rerank_enabled=False)


@dataclass(slots=True)
class Recording:
    """Delegating store that records what the runtime asks storage for."""

    inner: object
    limits: list[LexicalLimits] = field(default_factory=list[LexicalLimits])
    text_requests: list[int] = field(default_factory=list[int])
    vector_limits: list[int] = field(default_factory=list[int])
    scan_caps: list[int] = field(default_factory=list[int])
    pages: list[LexicalPage] = field(default_factory=list[LexicalPage])
    statements: int = 0

    def metadata_eligibility(
        self,
        scope: RetrievalScope,
        topics: tuple[str, ...],
        entity_ids: tuple[UUID, ...],
        scan_cap: int,
    ) -> MetadataEligibility:
        self.scan_caps.append(scan_cap)
        method = cast(
            "Callable[..., MetadataEligibility]",
            getattr(self.inner, "metadata_eligibility"),  # noqa: B009
        )
        return method(scope, topics, entity_ids, scan_cap)

    def lexical_chunks(
        self, query: str, scope: RetrievalScope, limits: LexicalLimits
    ) -> LexicalPage:
        self.limits.append(limits)
        method = cast(
            "Callable[..., LexicalPage]",
            getattr(self.inner, "lexical_chunks"),  # noqa: B009
        )
        page = method(query, scope, limits)
        self.pages.append(page)
        return page

    def chunk_texts(self, chunks: tuple[ChunkRef, ...]) -> tuple[str, ...]:
        self.text_requests.append(len(chunks))
        method = cast(
            "Callable[..., tuple[str, ...]]",
            getattr(self.inner, "chunk_texts"),  # noqa: B009
        )
        return method(chunks)

    def vector_chunks(
        self,
        embedding: tuple[float, ...],
        identity: EmbeddingIdentity,
        scope: RetrievalScope,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        self.vector_limits.append(limit)
        method = cast(
            "Callable[..., tuple[VectorHit, ...]]",
            getattr(self.inner, "vector_chunks"),  # noqa: B009
        )
        return method(embedding, identity, scope, limit)

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        method = cast(
            "Callable[..., tuple[CurrentArticleState, ...]]",
            getattr(self.inner, "current_articles"),  # noqa: B009
        )
        return method(article_ids)


def with_cap(path: str, value: float) -> RetrievalPolicy:
    """Copy of the bundled policy with one leaf changed (no re-validation)."""
    section_name, leaf = path.split(".")
    section = cast("BaseModel", getattr(POLICY, section_name))
    return POLICY.model_copy(
        update={section_name: section.model_copy(update={leaf: value})}
    )


def corpus(articles: int = 8, chunks: int = 3) -> MemoryStore:
    store = MemoryStore()
    start = datetime(2026, 3, 1, tzinfo=UTC)
    for index in range(articles):
        body = " ".join(f"u{index}w{j}" for j in range(chunks * 160))
        document = article(
            f"harbor crane {body}"[: 1200 + (chunks - 1) * 1000],
            title="Harbor crane notice",
            published=start + timedelta(hours=index),
        )
        store.add(document, vectors=True)
        store.metadata[document.article_id] = metadata_for(document)
    return store


def run(policy: RetrievalPolicy, **query: object) -> tuple[Recording, RetrievalResult]:
    recording = Recording(corpus())
    result = service(recording, policy=policy).execute(
        briefing("harbor crane", **query), CALLER, control()
    )
    return recording, result


def lexical_ranks(result: RetrievalResult) -> list[int]:
    return [
        span.lexical_rank
        for a in result.articles
        for span in a.spans
        if span.lexical_rank is not None
    ]


def check_query_terms(r: Recording, _: RetrievalResult) -> bool:
    return r.limits[0].max_terms == 1


def check_metadata_scan(r: Recording, _: RetrievalResult) -> bool:
    return r.scan_caps == [2]


def check_article_pool(r: Recording, _: RetrievalResult) -> bool:
    return (
        r.limits[0].article_pool == 2
        and len({c.article_id for c in r.pages[0].chunks}) <= 2
    )


def check_scan(r: Recording, _: RetrievalResult) -> bool:
    return r.limits[0].chunks_scanned_per_article == 1 and all(
        c.chunk_index == 0 for c in r.pages[0].chunks
    )


def check_per_article(r: Recording, result: RetrievalResult) -> bool:
    return r.limits[0].chunks_per_article == 1 and all(
        len(a.spans) >= 1 for a in result.articles
    )


def check_lexical_depth(_: Recording, result: RetrievalResult) -> bool:
    ranks = lexical_ranks(result)
    return bool(ranks) and max(ranks) <= 2


def check_text_chunks(r: Recording, _: RetrievalResult) -> bool:
    return r.text_requests == [2]


def check_vector_pool(r: Recording, _: RetrievalResult) -> bool:
    return r.vector_limits == [7]


def check_vector_depth(_: Recording, result: RetrievalResult) -> bool:
    ranks = [s.vector_rank for a in result.articles for s in a.spans if s.vector_rank]
    return bool(ranks) and max(ranks) <= 2


def check_vector_per_article(_: Recording, result: RetrievalResult) -> bool:
    return all(
        sum(1 for s in a.spans if s.vector_rank is not None) <= 1
        for a in result.articles
    )


def check_spans(_: Recording, result: RetrievalResult) -> bool:
    return all(len(a.spans) == 1 for a in result.articles)


def check_final_pool(_: Recording, result: RetrievalResult) -> bool:
    return len(result.articles) <= 2


def check_articles(_: Recording, result: RetrievalResult) -> bool:
    return len(result.articles) == 1


def check_tokens(_: Recording, result: RetrievalResult) -> bool:
    return result.context.token_budget == 300 and result.context.token_count <= 300


# path -> (changed value, extra query fields, observable check)
WIRING: dict[
    str, tuple[float, dict[str, object], Callable[[Recording, RetrievalResult], bool]]
] = {
    "tokenizer.max_query_terms": (1, {}, check_query_terms),
    "filters.metadata_scan_cap": (
        2,
        {"topic_filters": ("transportation",)},
        check_metadata_scan,
    ),
    "lexical.article_pool": (2, {}, check_article_pool),
    "lexical.max_chunks_scanned_per_article": (1, {}, check_scan),
    "lexical.max_chunks_per_article": (1, {}, check_per_article),
    "lexical.depth": (2, {}, check_lexical_depth),
    "lexical.max_text_chunks": (2, {}, check_text_chunks),
    "vector.pool": (7, {}, check_vector_pool),
    "vector.depth": (2, {}, check_vector_depth),
    "vector.max_chunks_per_article": (1, {}, check_vector_per_article),
    "aggregation.max_spans_per_article": (1, {}, check_spans),
    "aggregation.final_check_pool_multiplier": (
        1,
        {"result_limit": 2},
        check_final_pool,
    ),
    "output.max_articles": (1, {}, check_articles),
    "output.max_context_tokens": (300, {}, check_tokens),
}
# Caps proven by dedicated behavioral tests elsewhere (file::test).
COVERED_ELSEWHERE = {
    "freshness.half_life_days": "test_fusion.py::test_freshness_uses_fixed_clock_*",
    "freshness.weight": "test_fusion.py::test_freshness_uses_fixed_clock_*",
    "dedup.shingle_size": "test_fusion.py::test_near_duplicate_wire_copy_*",
    "dedup.near_duplicate_threshold": "test_fusion.py::test_near_duplicate_*",
    "lexical.max_metadata_candidates": "test_metadata_candidate_overflow_*",
    "rerank.max_candidates": "test_t5_policy_v3.py::test_rerank_caps_are_wired",
    "rerank.max_tokens_per_candidate": "test_t5_policy_v3.py::test_rerank_caps_*",
    "rerank.timeout_seconds": "test_reranker.py::test_reranker_timeout_degrades_*",
    "output.min_truncated_span_tokens": "test_t5_policy_v3.py::test_min_truncated_*",
}


def numeric_leaves(model: BaseModel, prefix: str = "") -> set[str]:
    leaves: set[str] = set()
    for name in type(model).model_fields:
        value = cast("object", getattr(model, name))
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            leaves |= numeric_leaves(value, f"{path}.")
        elif isinstance(value, int | float) and not isinstance(value, bool):
            leaves.add(path)
    return leaves


def test_every_numeric_v3_cap_is_wired_or_explicitly_covered() -> None:
    fixed_literals = {"rrf.k", "rrf.missing_branch_contribution"}  # Literal-typed
    leaves = numeric_leaves(POLICY) - fixed_literals

    assert POLICY.policy_id == "retrieval-policy.v3"
    assert leaves == set(WIRING) | set(COVERED_ELSEWHERE)


@pytest.mark.parametrize("path", sorted(WIRING))
def test_v3_cap_changes_runtime_behavior(path: str) -> None:
    value, query, check = WIRING[path]
    recording, result = run(with_cap(path, value), **query)

    assert check(recording, result), path


def test_metadata_candidate_overflow_is_a_typed_failure_not_a_silent_cut() -> None:
    recording = Recording(corpus())
    retrieve = service(recording, policy=with_cap("lexical.max_metadata_candidates", 1))

    with pytest.raises(RetrievalConfigurationError):
        _ = retrieve.execute(briefing("harbor crane"), CALLER, control(), LEXICAL)
    assert recording.limits[0].max_metadata_candidates == 1  # passed to storage
    assert recording.text_requests == []  # nothing selected or fetched


def test_rerank_caps_are_wired() -> None:
    requests: list[RerankRequest] = []

    def record(request: RerankRequest) -> RerankOutput:
        requests.append(request)
        return RerankOutput(
            scores=tuple((c.candidate_id, 1.0) for c in request.candidates)
        )

    policy = POLICY.model_copy(
        update={
            "rerank": POLICY.rerank.model_copy(
                update={"max_candidates": 3, "max_tokens_per_candidate": 2}
            )
        }
    )
    _ = service(corpus(), reranker=ScriptedReranker(record), policy=policy).execute(
        briefing("harbor crane"), CALLER, control()
    )

    assert len(requests[0].candidates) == 3
    assert all(len(c.text.split()) <= 2 for c in requests[0].candidates)


def test_min_truncated_span_tokens_is_wired() -> None:
    store = corpus(1)
    full = service(store, embeddings=None).execute(
        briefing("harbor crane"), CALLER, control(), LEXICAL
    )
    budget = full.context.token_count - 5  # forces truncation of the last span

    def result_for(minimum: int) -> RetrievalResult:
        policy = with_cap("output.min_truncated_span_tokens", minimum)
        return service(store, embeddings=None, policy=policy).execute(
            briefing("harbor crane", context_budget=budget),
            CALLER,
            control(),
            LEXICAL,
        )

    assert result_for(16).context.truncated_spans == 1  # last span cut to fit
    assert result_for(10_000).context.truncated_spans == 0  # cut not allowed


# Real PostgreSQL worst cases ----------------------------------------------------

STEP = 1000


def tied_document(index: int, windows: int) -> tuple[str, str]:
    length = 1200 + (windows - 1) * STEP
    body = " ".join(f"a{index}x{j}" for j in range(length // 4))[:length]
    return body, f"Harborword {index}"


def load(url: str, articles: int, windows: int) -> list[UUID]:
    store = PostgresRagStore(url)
    published = datetime(2026, 3, 1, tzinfo=UTC)
    ids: list[UUID] = []
    for index in range(articles):
        body, _ = tied_document(index, windows)
        document = article(
            body,
            article_id=UUID(int=index + 1),
            title="Harborword bulletin",  # title-only match -> every window ties
            published=published,
        )
        store.persist_article(document)
        ids.append(document.article_id)
    return ids


def measure(url: str) -> tuple[Recording, PostgresRetrievalStore, RetrievalResult]:
    inner = PostgresRetrievalStore(url)
    recording = Recording(inner)
    result = service(recording, embeddings=None).execute(
        briefing("harborword", result_limit=20), CALLER, control(), LEXICAL
    )
    return recording, inner, result


def test_50_articles_x_40_tied_windows_hit_exactly_2000_metadata_rows(
    retrieval_url: str,
) -> None:
    _ = load(retrieval_url, 50, 40)

    recording, inner, result = measure(retrieval_url)

    assert len(recording.pages[0].chunks) == 2000  # article_pool x scan, ties kept
    assert recording.text_requests == [50]  # max_text_chunks
    assert recording.pages[0].truncated_articles == 0
    assert result.metrics.lexical_truncated_articles == 0
    assert inner.statements == 3  # metadata + texts + one final batch
    assert all(len(a.spans) <= 3 for a in result.articles)


def test_51st_article_and_41st_window_are_bounded_deterministically(
    retrieval_url: str,
) -> None:
    ids = load(retrieval_url, 51, 41)

    recording, inner, result = measure(retrieval_url)

    page = recording.pages[0]
    assert len(page.chunks) == 2000
    assert {c.article_id for c in page.chunks} == set(ids[:50])  # article_id ASC
    assert all(c.chunk_index < 40 for c in page.chunks)  # window 41 never scanned
    assert page.truncated_articles == 50
    assert result.metrics.lexical_truncated_articles == 50
    assert recording.text_requests == [50]
    assert inner.statements == 3
    assert ids[50] not in {a.article_id for a in result.articles}


def test_v1_and_v2_policies_stay_byte_identical_history() -> None:
    policies = Path(__file__).parents[4] / "src/backend/rag/application/policies"
    digests = {
        name: hashlib.sha256((policies / name).read_bytes()).hexdigest()
        for name in ("retrieval-policy.v1.json", "retrieval-policy.v2.json")
    }

    assert digests == {
        "retrieval-policy.v1.json": (
            "34d25ee70c990936ef2aa542822c0622f779a39ad24e6d22720b57ccf1639cb6"
        ),
        "retrieval-policy.v2.json": (
            "add4d6cf273a986031f7565309848d7c4a8e9cbc0914147d4936af84e6aded59"
        ),
    }
