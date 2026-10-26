"""T5 hybrid retrieval on real PostgreSQL/pgvector (fresh schema per test).

Tests needing ``retrieval_url`` skip without EVENTLENS_RAG_DATABASE_URL in
``make check``; ``make rag-db-test`` runs them against an isolated container
and fails on any skip.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg.conninfo import make_conninfo
from pydantic import TypeAdapter

from backend.rag.application.ports.reranker import RerankOutput, RerankRequest
from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.application.use_cases.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity, EmbeddingVector
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity, OutcomeKind
from backend.rag.domain.retrieval import (
    CoverageStatus,
    EmptyReason,
    RerankStatus,
    ResultStatus,
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalMode,
    RetrievalResult,
    RetrievalStorageError,
    VectorBranchStatus,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.retrieval.scripted_reranker import (
    ScriptedReranker,
    reverse_script,
)
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.shared.contracts.article import Article
from retrieval_support import (
    CALLER,
    MutableAccess,
    article,
    briefing,
    control,
    execute,
    metadata_for,
    publish_ready,
    service,
    stage,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)
BUNDLE = Path(__file__).parents[4] / "fixtures/article_bundle.v1.json"
LEXICAL = RetrievalOptions(vector_enabled=False)
VECTOR_ONLY = RetrievalOptions(lexical_enabled=False)


def persist(url: str, *documents: ArticleDocument) -> None:
    store = PostgresRagStore(url)
    for document in documents:
        store.persist_article(document)


def ids(result: RetrievalResult) -> set[UUID]:
    return {item.article_id for item in result.articles}


def retrieve(
    url: str,
    query: str = "harbor crane",
    options: RetrievalOptions | None = None,
    **overrides: object,
) -> RetrievalResult:
    return service(PostgresRetrievalStore(url)).execute(
        briefing(query, **overrides), CALLER, control(), options
    )


def harbor(text: str = "", **kwargs: object) -> ArticleDocument:
    # Distinct filler keeps test articles below the near-duplicate threshold.
    filler = " ".join(f"w{char}{index}" for index, char in enumerate(uuid4().hex))
    return article(
        f"Harbor crane inspection closes the East Pier gate. {text} {filler}.",
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


# 1. lexical before READY, hybrid after READY, lexical for PENDING/FAILED/STALE


def test_same_query_is_lexical_only_before_ready_and_hybrid_after(
    retrieval_url: str,
) -> None:
    document = harbor(published=T0)
    persist(retrieval_url, document)

    before = retrieve(retrieval_url)
    _, generation = publish_ready(retrieval_url, document)
    after = retrieve(retrieval_url)

    assert ids(before) == ids(after) == {document.article_id}
    assert before.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert before.articles[0].vector_generation is None
    assert before.articles[0].metadata is None
    assert after.retrieval_mode is RetrievalMode.HYBRID
    assert after.vector_branch_status is VectorBranchStatus.EXECUTED
    first = after.articles[0]
    assert first.retrieval_basis is RetrievalMode.HYBRID
    assert first.vector_generation == generation
    assert first.metadata is not None
    assert first.metadata.index_generation == generation
    span = first.spans[0].span
    assert document.normalized_content[span.start_offset : span.end_offset] == (
        span.excerpt
    )
    assert after.provenance.index_version.startswith("retrieval-policy.v3|")


def test_pending_failed_and_stale_enrichment_keep_lexical_hits(
    retrieval_url: str,
) -> None:
    pending, failed, corrected = harbor("one"), harbor("two"), harbor("three")
    persist(retrieval_url, pending, failed, corrected)
    store = PostgresRagStore(retrieval_url)
    _ = store.enqueue_job(pending.article_id, pending.content_hash, "v1", "v1", "v1")
    failed_job = store.enqueue_job(
        failed.article_id, failed.content_hash, "v1", "v1", "v1"
    )
    execute(
        retrieval_url,
        "UPDATE rag_enrichment_jobs SET status = 'FAILED' WHERE job_id = %s",
        (failed_job,),
    )
    _ = publish_ready(retrieval_url, corrected)
    revision = article(
        corrected.normalized_content + " Corrected.", article_id=corrected.article_id
    )
    persist(retrieval_url, revision)  # old READY job -> STALE, vectors cleared

    result = retrieve(retrieval_url)

    assert ids(result) == {pending.article_id, failed.article_id, revision.article_id}
    assert result.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    by_id = {a.article_id: a for a in result.articles}
    assert by_id[revision.article_id].content_hash == revision.content_hash
    assert by_id[revision.article_id].metadata is None
    assert all(a.vector_generation is None for a in result.articles)


# 3. metadata filters, coverage and semantics


def test_topic_filter_never_false_matches_missing_metadata(retrieval_url: str) -> None:
    ready, other_topic, unenriched = harbor("a"), harbor("b"), harbor("c")
    persist(retrieval_url, ready, other_topic, unenriched)

    unavailable = retrieve(retrieval_url, topic_filters=("transportation",))
    _ = publish_ready(retrieval_url, ready)
    _ = publish_ready(
        retrieval_url, other_topic, metadata_for(other_topic, ("economy",))
    )
    filtered = retrieve(retrieval_url, topic_filters=("transportation",))
    unfiltered = retrieve(retrieval_url)

    assert unavailable.status is ResultStatus.EMPTY
    assert unavailable.empty_reason is EmptyReason.FILTER_UNAVAILABLE
    assert unavailable.coverage.status is CoverageStatus.FILTER_UNAVAILABLE
    assert ids(filtered) == {ready.article_id}
    assert filtered.coverage.status is CoverageStatus.PARTIAL
    assert filtered.coverage.unverifiable_articles == 1
    assert filtered.coverage.verifiable_articles == 2
    assert ids(unfiltered) == {
        ready.article_id,
        other_topic.article_id,
        unenriched.article_id,
    }
    assert unfiltered.coverage.metadata_filter_requested is False


def test_topic_any_of_entity_any_of_and_all_across_kinds(retrieval_url: str) -> None:
    entity_a, entity_b = uuid4(), uuid4()
    a, b, c = harbor("a"), harbor("b"), harbor("c")
    persist(retrieval_url, a, b, c)
    _ = publish_ready(retrieval_url, a, metadata_for(a, ("transportation",), entity_a))
    _ = publish_ready(retrieval_url, b, metadata_for(b, ("economy",), entity_b))
    _ = publish_ready(retrieval_url, c, metadata_for(c, ("transportation", "economy")))

    any_topic = retrieve(retrieval_url, topic_filters=("transportation", "economy"))
    both_kinds = retrieve(
        retrieval_url, topic_filters=("transportation",), entity_ids=(entity_a,)
    )
    entity_only = retrieve(retrieval_url, entity_ids=(entity_b, uuid4()))
    mismatch = retrieve(
        retrieval_url, topic_filters=("transportation",), entity_ids=(entity_b,)
    )

    assert ids(any_topic) == {a.article_id, b.article_id, c.article_id}
    assert ids(both_kinds) == {a.article_id}
    assert ids(entity_only) == {b.article_id}
    assert mismatch.status is ResultStatus.EMPTY
    assert mismatch.empty_reason is EmptyReason.NO_MATCHES
    assert all(
        r.coverage.status is CoverageStatus.COMPLETE
        for r in (any_topic, both_kinds, entity_only)
    )


@pytest.mark.parametrize("options", [LEXICAL, VECTOR_ONLY, RetrievalOptions()])
def test_publication_bounds_are_inclusive_exclusive_and_exclude_nulls(
    retrieval_url: str, options: RetrievalOptions
) -> None:
    t1, t2 = T0 + timedelta(days=1), T0 + timedelta(days=2)
    docs = {
        "t0": harbor("zero", published=T0),
        "t1": harbor("one", published=t1),
        "t2": harbor("two", published=t2),
        "none": harbor("null", published=None),
    }
    persist(retrieval_url, *docs.values())
    for document in docs.values():
        _ = publish_ready(retrieval_url, document)

    def window(**bounds: object) -> set[str]:
        result = service(PostgresRetrievalStore(retrieval_url)).execute(
            briefing("harbor crane", **bounds), CALLER, control(), options
        )
        names = {v.article_id: k for k, v in docs.items()}
        return {names[i] for i in ids(result)}

    assert window() == {"t0", "t1", "t2", "none"}
    assert window(published_after=t1) == {"t1", "t2"}
    assert window(published_before=t1) == {"t0"}
    assert window(published_after=T0, published_before=t2) == {"t0", "t1"}
    assert window(published_after=t2 + timedelta(seconds=1)) == set()


def test_equal_bounds_are_an_empty_range_without_storage_work(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor(published=T0))
    store = PostgresRetrievalStore(retrieval_url)

    result = service(store).execute(
        briefing("harbor", published_after=T0, published_before=T0),
        CALLER,
        control(),
    )

    assert result.empty_reason is EmptyReason.EMPTY_PUBLICATION_RANGE
    assert store.statements == 0


@pytest.mark.parametrize("options", [LEXICAL, VECTOR_ONLY])
def test_source_domain_filter_applies_in_each_branch_without_backfill(
    retrieval_url: str, options: RetrievalOptions
) -> None:
    inside = harbor("in", domain="a.example")
    outside = [harbor(f"out {i}", domain="b.example") for i in range(5)]
    persist(retrieval_url, inside, *outside)
    for document in (inside, *outside):
        _ = publish_ready(retrieval_url, document)

    result = retrieve(
        retrieval_url, options=options, source_domains=("a.example",), result_limit=5
    )

    assert ids(result) == {inside.article_id}
    assert all(a.source_domain == "a.example" for a in result.articles)


def test_fixture_denial_raises_before_any_storage_call(retrieval_url: str) -> None:
    persist(retrieval_url, harbor())
    store = PostgresRetrievalStore(retrieval_url)

    with pytest.raises(RetrievalAccessDeniedError):
        _ = service(store, access=MutableAccess(revoked=True)).execute(
            briefing("harbor"), CALLER, control()
        )
    assert store.statements == 0


# 4. embedding identity, staged/incomplete generations, metadata history


def test_incompatible_corpus_generation_finds_no_vectors(retrieval_url: str) -> None:
    document = harbor()
    persist(retrieval_url, document)
    _ = publish_ready(retrieval_url, document)
    other = EmbeddingIdentity(corpus_generation="rag-fixture-corpus.v2")

    result = service(PostgresRetrievalStore(retrieval_url), identity=other).execute(
        briefing("harbor crane"), CALLER, control()
    )

    assert result.vector_branch_status is VectorBranchStatus.EXECUTED
    assert result.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert result.articles[0].vector_generation is None


class WrongVersionEmbeddings:
    def embed(self, text: str) -> EmbeddingVector:
        vector = FakeEmbeddings().embed(text)
        return EmbeddingVector(vector.model, "fixture-embedding.v2", vector.values)


def test_incompatible_query_embedding_falls_back_to_lexical_only(
    retrieval_url: str,
) -> None:
    document = harbor()
    persist(retrieval_url, document)
    _ = publish_ready(retrieval_url, document)
    store = PostgresRetrievalStore(retrieval_url)

    result = service(store, embeddings=WrongVersionEmbeddings()).execute(
        briefing("harbor crane"), CALLER, control()
    )

    assert result.vector_branch_status is VectorBranchStatus.EMBEDDING_INCOMPATIBLE
    assert result.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert store.statements == 3  # lexical + chunk texts + final, no vector


def test_staged_and_incomplete_generations_are_never_searched(
    retrieval_url: str,
) -> None:
    staged_only = harbor("staged " + "x" * 1500)
    incomplete = harbor("incomplete " + "y" * 1500)
    persist(retrieval_url, staged_only, incomplete)
    _ = stage(retrieval_url, staged_only)  # STAGING, never activated
    generation = stage(retrieval_url, incomplete)
    assert PostgresStaging(retrieval_url).activate(generation)
    vector_only = retrieve(retrieval_url, options=VECTOR_ONLY)
    assert ids(vector_only) == {incomplete.article_id}

    execute(
        retrieval_url,
        "DELETE FROM rag_chunks WHERE generation = %s AND chunk_index = 1",
        (generation,),
    )
    after_damage = retrieve(retrieval_url, options=VECTOR_ONLY)

    assert after_damage.status is ResultStatus.EMPTY
    assert after_damage.retrieval_mode is RetrievalMode.NONE


def test_only_current_publication_counts_never_history(retrieval_url: str) -> None:
    document = harbor()
    persist(retrieval_url, document)
    _ = publish_ready(retrieval_url, document, metadata_for(document, version="v1"))
    _ = publish_ready(
        retrieval_url,
        document,
        metadata_for(document, ("economy",), version="v2"),
    )

    old_topic = retrieve(retrieval_url, topic_filters=("transportation",))
    new_topic = retrieve(retrieval_url, topic_filters=("economy",))

    assert old_topic.status is ResultStatus.EMPTY
    assert ids(new_topic) == {document.article_id}
    metadata = new_topic.articles[0].metadata
    assert metadata is not None
    assert metadata.enrichment_version == "v2"

    correction = article(
        document.normalized_content + " Updated.", article_id=document.article_id
    )
    persist(retrieval_url, correction)
    after_correction = retrieve(retrieval_url, topic_filters=("economy",))
    assert after_correction.empty_reason is EmptyReason.FILTER_UNAVAILABLE


# 7. races between retrieval steps (Event-free: the scripted reranker is the barrier)


def race(url: str, mutate: Callable[[], None], **overrides: object) -> RetrievalResult:
    def script(request: RerankRequest) -> RerankOutput:
        mutate()
        return reverse_script(request)

    return service(
        PostgresRetrievalStore(url), reranker=ScriptedReranker(script)
    ).execute(briefing("harbor crane", **overrides), CALLER, control())


def test_correction_during_retrieval_never_returns_old_spans(
    retrieval_url: str,
) -> None:
    changed, stable = harbor("changing"), harbor("stable")
    persist(retrieval_url, changed, stable)
    revision = article("Completely rewritten text.", article_id=changed.article_id)

    result = race(retrieval_url, lambda: persist(retrieval_url, revision))

    assert ids(result) == {stable.article_id}
    assert result.metrics.dropped_at_final_check == 1
    assert changed.content_hash not in {a.content_hash for a in result.articles}


def test_tombstone_during_retrieval_drops_article(retrieval_url: str) -> None:
    gone, stable = harbor("gone"), harbor("stable")
    persist(retrieval_url, gone, stable)

    result = race(
        retrieval_url,
        lambda: PostgresRagStore(retrieval_url).tombstone_article(gone.article_id),
    )

    assert ids(result) == {stable.article_id}


def test_metadata_supersession_during_filtered_retrieval(retrieval_url: str) -> None:
    moving, stable = harbor("moving"), harbor("stable")
    persist(retrieval_url, moving, stable)
    _ = publish_ready(retrieval_url, moving)
    _ = publish_ready(retrieval_url, stable)

    def supersede() -> None:
        _ = publish_ready(
            retrieval_url, moving, metadata_for(moving, ("economy",), version="v2")
        )

    result = race(retrieval_url, supersede, topic_filters=("transportation",))

    assert ids(result) == {stable.article_id}
    assert result.metrics.dropped_at_final_check == 1


def test_vector_only_candidate_from_replaced_generation_is_dropped(
    retrieval_url: str,
) -> None:
    document = harbor()
    persist(retrieval_url, document)
    _ = publish_ready(retrieval_url, document)

    def reactivate() -> None:
        other = EmbeddingIdentity(corpus_generation="rag-fixture-corpus.v9")
        generation = stage(retrieval_url, document, other)
        assert PostgresStaging(retrieval_url).activate(generation)

    def script(request: RerankRequest) -> RerankOutput:
        reactivate()
        return reverse_script(request)

    result = service(
        PostgresRetrievalStore(retrieval_url), reranker=ScriptedReranker(script)
    ).execute(briefing("harbor crane"), CALLER, control(), VECTOR_ONLY)

    assert result.empty_reason is EmptyReason.DROPPED_AT_FINAL_CHECK


@pytest.mark.parametrize("options", [LEXICAL, RetrievalOptions()])
def test_final_check_is_batched_without_n_plus_one(
    retrieval_url: str, options: RetrievalOptions
) -> None:
    documents = [harbor(f"number {i}", published=T0) for i in range(25)]
    persist(retrieval_url, *documents)
    for document in documents[:3]:
        _ = publish_ready(retrieval_url, document)
    counts: list[int] = []
    for limit in (3, 20):
        store = PostgresRetrievalStore(retrieval_url)
        result = service(store).execute(
            briefing("harbor crane", result_limit=limit), CALLER, control(), options
        )
        assert len(result.articles) == limit
        counts.append(store.statements)

    # lexical candidates + chunk texts (+ vector) + one final check
    expected = 3 if options.vector_enabled is False else 4
    assert counts == [expected, expected]


# 8. cancellation and storage failures


def test_pre_cancelled_query_starts_no_work(retrieval_url: str) -> None:
    persist(retrieval_url, harbor())
    store = PostgresRetrievalStore(retrieval_url)
    ctl = control()
    ctl.cancel()

    with pytest.raises(RetrievalCancelledError):
        _ = service(store).execute(briefing("harbor"), CALLER, ctl)
    assert store.statements == 0


def test_cancel_during_embedding_starts_no_vector_query(retrieval_url: str) -> None:
    persist(retrieval_url, harbor())
    store = PostgresRetrievalStore(retrieval_url)
    ctl = control()

    class CancellingEmbeddings:
        def embed(self, text: str) -> EmbeddingVector:
            ctl.cancel()  # in-flight call finishes, nothing new may start
            return FakeEmbeddings().embed(text)

    with pytest.raises(RetrievalCancelledError):
        _ = service(store, embeddings=CancellingEmbeddings()).execute(
            briefing("harbor"), CALLER, ctl
        )
    assert store.statements == 2  # lexical reads admitted before the cancel


def test_embedding_provider_failure_is_lexical_only_not_semantic(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor())

    class BrokenEmbeddings:
        def embed(self, text: str) -> EmbeddingVector:
            raise TimeoutError(text[:0] + "provider timeout")

    result = service(
        PostgresRetrievalStore(retrieval_url), embeddings=BrokenEmbeddings()
    ).execute(briefing("harbor"), CALLER, control())

    assert result.vector_branch_status is VectorBranchStatus.EMBEDDING_FAILED
    assert result.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert result.status is ResultStatus.EVIDENCE


def test_missing_schema_is_typed_storage_failure_not_empty(database_url: str) -> None:
    broken = make_conninfo(database_url, options="-c search_path=t5_missing_schema")

    with pytest.raises(RetrievalStorageError):
        _ = service(PostgresRetrievalStore(broken)).execute(
            briefing("harbor"), CALLER, control()
        )


def test_unreachable_database_is_typed_storage_failure() -> None:
    store = PostgresRetrievalStore(
        "postgresql://nobody:secret@127.0.0.1:1/missing?connect_timeout=1"
    )
    retrieve_evidence: RetrieveEvidence = service(store)

    with pytest.raises(RetrievalStorageError) as error:
        _ = retrieve_evidence.execute(briefing("harbor"), CALLER, control())
    assert "secret" not in str(error.value)
    assert error.value.code == "retrieval_storage_unavailable"


def test_rerank_degradation_is_not_used_for_storage_errors(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor())

    def break_storage(request: RerankRequest) -> RerankOutput:
        # Rename (not drop): search_path would fall back to public.* copies.
        execute(
            retrieval_url,
            "ALTER TABLE rag_article_current RENAME COLUMN tombstoned_at TO gone_at",
        )
        return reverse_script(request)

    with pytest.raises(RetrievalStorageError):
        _ = service(
            PostgresRetrievalStore(retrieval_url),
            reranker=ScriptedReranker(break_storage),
        ).execute(briefing("harbor"), CALLER, control())


# 9. ingest -> lexical -> fake enrich -> READY -> filtered retrieval -> evidence


def test_end_to_end_ingest_enrich_filtered_retrieval(retrieval_url: str) -> None:
    source = TypeAdapter(dict[str, object]).validate_json(BUNDLE.read_bytes())
    upstream = Article.model_validate_json(
        TypeAdapter(object).dump_json(source["article"])
    )
    enricher = FakeArticleEnricher()
    identity = EnrichmentIdentity(
        ENRICHMENT_VERSION, enricher.model_version, enricher.prompt_version
    )
    repository = PostgresEnrichmentRepository(retrieval_url)
    receipt = IngestArticle(
        repository, FixtureAccessPolicy(), identity, lambda: datetime.now(UTC)
    ).execute(upstream, CALLER)
    assert receipt.job_id is not None

    lexical = retrieve(retrieval_url, "công viên")
    pending_filter = retrieve(retrieval_url, "công viên", topic_filters=("other",))
    steps = EnrichmentSteps(
        repository=repository,
        enricher=enricher,
        generations=EnsureGeneration(
            PostgresStaging(retrieval_url), FakeEmbeddings(), EmbeddingIdentity()
        ),
        policy=bundled_retry_policy(),
        clock=lambda: datetime.now(UTC),
    )
    report = EnrichmentRunner(steps, RecordingTelemetry()).run(5)
    filtered = retrieve(retrieval_url, "công viên", topic_filters=("other",))
    wrong_topic = retrieve(retrieval_url, "công viên", topic_filters=("weather",))

    assert lexical.retrieval_mode is RetrievalMode.LEXICAL_ONLY
    assert ids(lexical) == {upstream.article_id}
    assert pending_filter.empty_reason is EmptyReason.FILTER_UNAVAILABLE
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]
    assert ids(filtered) == {upstream.article_id}
    assert filtered.retrieval_mode is RetrievalMode.HYBRID
    assert filtered.rerank_status is RerankStatus.APPLIED
    assert wrong_topic.status is ResultStatus.EMPTY
    first = filtered.articles[0]
    assert first.metadata is not None
    assert first.metadata.model_version == enricher.model_version
    assert first.source_domain == "vneconomy.vn"
    span = first.spans[0].span
    assert span.excerpt.startswith("Thành phố")
    assert filtered.context.token_count <= filtered.context.token_budget
    assert "s3://" not in filtered.context.text
