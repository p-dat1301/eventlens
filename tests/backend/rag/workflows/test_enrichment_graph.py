"""Real compiled enrichment graph over in-memory fakes (unit level, no DB)."""

from uuid import UUID

import pytest

from backend.rag.application.ports.enrichment import (
    EnrichmentProviderError,
    EnrichmentRequest,
)
from backend.rag.application.use_cases.enrichment import CancellationScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.enrichment_draft import EnrichmentDraft, PromptContext
from backend.rag.domain.enrichment_lifecycle import (
    CancelAck,
    FailureCode,
    OutcomeKind,
    PublishResult,
)
from backend.rag.infra.llm.fake_enricher import (
    FakeArticleEnricher,
    ScriptedArticleEnricher,
)
from backend.rag.infra.telemetry.telemetry import JsonLinesTelemetry, RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import (
    EnrichmentGraph,
    EnrichmentRunner,
    InvalidPassSizeError,
)
from backend.shared.contracts import EnrichmentStatus
from enrichment_support import (
    MemoryWorld,
    document,
    grounded_draft,
    identity_of,
)

S = EnrichmentStatus
CONTENT = "The Harbor Authority closed pier 4 after a storm. Ferries resume Monday."
TIMEOUT = EnrichmentProviderError(FailureCode.PROVIDER_TIMEOUT)


def ingest(world: MemoryWorld, enricher: object, content: str = CONTENT) -> UUID:
    article = document(content)
    key = identity_of(enricher).key_for(article.article_id, article.content_hash)  # pyright: ignore[reportArgumentType]
    receipt = world.repository.ingest(article, key)
    assert receipt.job_id is not None
    return receipt.job_id


def test_happy_path_publishes_ready_metadata_and_vectors_atomically() -> None:
    world = MemoryWorld()
    enricher = FakeArticleEnricher()
    job = ingest(world, enricher)
    telemetry = RecordingTelemetry()
    report = EnrichmentRunner(world.steps(enricher), telemetry).run(5)
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]
    row = world.repository.job(job)
    assert row.status is S.READY
    assert row.attempt_count == 1
    (published,) = world.repository.publications
    assert published[3].topics == ("transportation", "weather")
    assert world.repository.active_generation[published[3].article_id] == published[2]
    assert world.embeddings.calls == 1
    assert telemetry.outcomes == list(report.outcomes)


def test_telemetry_records_ids_and_codes_never_article_text() -> None:
    world = MemoryWorld()
    lines: list[str] = []
    _ = ingest(world, FakeArticleEnricher())
    _ = EnrichmentRunner(
        world.steps(FakeArticleEnricher()), JsonLinesTelemetry(lines.append)
    ).run(1)
    assert len(lines) == 1
    assert "Harbor" not in lines[0]
    assert "storm" not in lines[0]
    assert '"outcome": "READY"' in lines[0]


def test_duplicate_delivery_creates_no_second_job_or_publication() -> None:
    world = MemoryWorld()
    enricher = FakeArticleEnricher()
    first = ingest(world, enricher)
    runner = EnrichmentRunner(world.steps(enricher), RecordingTelemetry())
    _ = runner.run(5)
    article = document(CONTENT)
    again = world.repository.ingest(
        article.model_copy(
            update={"article_id": world.repository.job(first).key.article_id}
        ),
        world.repository.job(first).key,
    )
    assert again.job_id == first
    assert not again.job_created
    assert runner.run(5).outcomes == ()
    assert len(world.repository.publications) == 1


def test_metadata_only_prompt_revision_reuses_embeddings_and_supersedes() -> None:
    world = MemoryWorld()
    first = ingest(world, FakeArticleEnricher())
    _ = EnrichmentRunner(world.steps(FakeArticleEnricher()), RecordingTelemetry()).run(
        1
    )
    calls, stages = world.embeddings.calls, world.staging.stage_calls
    revised = FakeArticleEnricher(prompt_version="enrichment-prompt.v2")
    report = EnrichmentRunner(world.steps(revised), RecordingTelemetry()).run(5)
    (outcome,) = report.outcomes
    assert report.reconciled == 1
    assert outcome.kind is OutcomeKind.READY
    assert outcome.embedding_reused is True
    assert (world.embeddings.calls, world.staging.stage_calls) == (calls, stages)
    assert outcome.job_id != first
    assert world.repository.job(first).status is S.STALE
    assert [p[3].prompt_version for p in world.repository.publications] == [
        "enrichment-prompt.v1",
        "enrichment-prompt.v2",
    ]


def test_model_version_change_alone_creates_distinct_job() -> None:
    world = MemoryWorld()
    first = ingest(world, FakeArticleEnricher())
    other = FakeArticleEnricher(model_version="fake-enricher.v2")
    report = EnrichmentRunner(world.steps(other), RecordingTelemetry()).run(5)
    (outcome,) = report.outcomes
    assert outcome.job_id != first
    assert world.repository.job(outcome.job_id).key.model_version == "fake-enricher.v2"
    assert world.repository.job(outcome.job_id).key.prompt_version == (
        world.repository.job(first).key.prompt_version
    )


def test_worker_never_claims_a_job_of_a_different_identity() -> None:
    world = MemoryWorld()
    job = ingest(world, FakeArticleEnricher(model_version="other-model"))
    steps = world.steps(FakeArticleEnricher())
    assert steps.claim() is None
    assert world.repository.job(job).status is S.PENDING


def test_transport_retry_follows_schedule_then_exhausts_with_one_call_each() -> None:
    world = MemoryWorld()
    enricher = ScriptedArticleEnricher([TIMEOUT, TIMEOUT, TIMEOUT])
    job = ingest(world, enricher)
    runner = EnrichmentRunner(world.steps(enricher), RecordingTelemetry())
    first = runner.run(5).outcomes
    assert [(o.kind, o.retry_in_seconds) for o in first] == [
        (OutcomeKind.RETRY_SCHEDULED, 30)
    ]
    world.clock.advance(29)
    assert runner.run(5).outcomes == ()
    world.clock.advance(1)
    second = runner.run(5).outcomes
    assert [(o.kind, o.attempt, o.retry_in_seconds) for o in second] == [
        (OutcomeKind.RETRY_SCHEDULED, 2, 120)
    ]
    world.clock.advance(120)
    (last,) = runner.run(5).outcomes
    assert (last.kind, last.attempt, last.failure_code) == (
        OutcomeKind.FAILED,
        3,
        FailureCode.PROVIDER_TIMEOUT,
    )
    assert len(enricher.requests) == 3
    assert world.repository.job(job).status is S.FAILED
    world.clock.advance(10_000)
    assert runner.run(5).outcomes == ()
    article_id = world.repository.job(job).key.article_id
    assert (
        world.repository.current[article_id]
        == world.repository.job(job).key.content_hash
    )


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (
            EnrichmentProviderError(FailureCode.MALFORMED_OUTPUT),
            FailureCode.MALFORMED_OUTPUT,
        ),
        (
            grounded_draft(CONTENT).model_copy(update={"topics": ("sports",)}),
            FailureCode.UNSUPPORTED_TAXONOMY,
        ),
        (
            grounded_draft(CONTENT).model_copy(
                update={"evidence": (grounded_draft("x Harbor Authority").evidence)}
            ),
            FailureCode.UNGROUNDED_EVIDENCE,
        ),
    ],
)
def test_output_failures_become_failed_without_retry(
    script: EnrichmentDraft | EnrichmentProviderError, code: FailureCode
) -> None:
    world = MemoryWorld()
    enricher = ScriptedArticleEnricher([script])
    job = ingest(world, enricher)
    runner = EnrichmentRunner(world.steps(enricher), RecordingTelemetry())
    (outcome,) = runner.run(5).outcomes
    assert (outcome.kind, outcome.failure_code) == (OutcomeKind.FAILED, code)
    world.clock.advance(10_000)
    assert runner.run(5).outcomes == ()
    assert len(enricher.requests) == 1
    assert world.repository.job(job).status is S.FAILED
    assert world.repository.publications == []
    assert world.staging.stage_calls == 0


def test_expired_owner_cannot_write_after_reclaim_and_new_owner_completes() -> None:
    world = MemoryWorld()
    enricher = FakeArticleEnricher()
    _ = ingest(world, enricher)
    steps = world.steps(enricher)
    scope = CancellationScope()
    worker_a = steps.claim()
    assert worker_a is not None
    article = steps.load(worker_a, scope)
    assert isinstance(article, ArticleDocument)
    world.clock.advance(steps.policy.lease_seconds)
    worker_b = steps.claim()
    assert worker_b is not None
    assert worker_b.claim_generation == worker_a.claim_generation + 1
    assert worker_b.attempt == 2
    metadata = steps.validate(
        worker_a, article, FakeArticleEnricher().enrich(_request(article)), scope
    )
    staged = steps.stage(worker_a, article, scope)
    late = steps.publish(worker_a, staged, metadata, scope)  # pyright: ignore[reportArgumentType]
    assert late.kind is OutcomeKind.LOST_OWNERSHIP
    assert steps.fail(worker_a, FailureCode.PROVIDER_TIMEOUT, scope).kind is (
        OutcomeKind.LOST_OWNERSHIP
    )
    assert world.repository.publications == []
    done = EnrichmentGraph(steps).invoke(worker_b, CancellationScope())
    assert done.kind is OutcomeKind.READY


def test_lease_expiry_without_reclaim_still_fences_late_owner() -> None:
    world = MemoryWorld()
    enricher = ScriptedArticleEnricher([])
    job = ingest(world, enricher)
    steps = world.steps(enricher)

    def slow(request: EnrichmentRequest) -> EnrichmentDraft:
        world.clock.advance(steps.policy.lease_seconds + 1)
        return grounded_draft(request.context.normalized_content)

    enricher.script.append(slow)
    claim = steps.claim()
    assert claim is not None
    outcome = EnrichmentGraph(steps).invoke(claim, CancellationScope())
    assert outcome.kind is OutcomeKind.LOST_OWNERSHIP
    assert world.repository.job(job).status is S.PROCESSING
    assert world.repository.publications == []


def test_repeated_lease_expiry_consumes_budget_then_fails() -> None:
    world = MemoryWorld()
    enricher = FakeArticleEnricher()
    job = ingest(world, enricher)
    steps = world.steps(enricher)
    for attempt in (1, 2, 3):
        claim = steps.claim()
        assert claim is not None
        assert claim.attempt == attempt
        world.clock.advance(steps.policy.lease_seconds)
    assert steps.claim() is None
    row = world.repository.job(job)
    assert (row.status, row.failure_code) == (S.FAILED, FailureCode.LEASE_EXPIRED)


@pytest.mark.parametrize("change", ["correction", "tombstone", "supersession"])
def test_change_while_processing_blocks_old_publication(change: str) -> None:
    world = MemoryWorld()
    enricher = ScriptedArticleEnricher([])
    job = ingest(world, enricher)
    article_id = world.repository.job(job).key.article_id

    def mutate(request: EnrichmentRequest) -> EnrichmentDraft:
        if change == "correction":
            corrected = document(CONTENT + " Update: pier 5 also closed.", article_id)
            world.repository.persist(corrected)
        elif change == "tombstone":
            world.repository.tombstone(article_id)
        else:
            newer = identity_of(FakeArticleEnricher(prompt_version="p-new"))
            key = newer.key_for(article_id, request.key.content_hash)
            _ = world.repository.ingest(document(CONTENT, article_id), key)
        return grounded_draft(request.context.normalized_content)

    enricher.script.append(mutate)
    (outcome,) = (
        EnrichmentRunner(world.steps(enricher), RecordingTelemetry()).run(1).outcomes
    )
    assert outcome.kind is OutcomeKind.LOST_OWNERSHIP
    assert world.repository.publications == []
    assert article_id not in world.repository.active_generation
    # Tombstones are swept to STALE by the next claim pass (same as PostgreSQL).
    _ = world.steps(enricher).claim()
    assert world.repository.job(job).status is S.STALE


def test_cancel_acknowledged_before_publication_stops_all_further_work() -> None:
    world = MemoryWorld()
    scope = CancellationScope()
    acks: list[CancelAck | None] = []
    enricher = ScriptedArticleEnricher([])

    def cancel_during_call(request: EnrichmentRequest) -> EnrichmentDraft:
        acks.append(scope.cancel())
        return grounded_draft(request.context.normalized_content)

    enricher.script.append(cancel_during_call)
    job = ingest(world, enricher)
    _ = ingest(world, enricher, CONTENT + " second article")
    steps = world.steps(enricher)
    report = EnrichmentRunner(steps, RecordingTelemetry()).run(5, scope)
    assert acks == [CancelAck.ACKNOWLEDGED]
    assert [o.kind for o in report.outcomes] == [OutcomeKind.CANCELLED]
    assert report.cancelled
    calls = world.repository.calls
    assert calls[calls.index("request_cancel") + 1 :] == []
    assert world.staging.stage_calls == 0
    assert world.embeddings.calls == 0
    assert len(enricher.requests) == 1
    row = world.repository.job(job)
    assert row.status is S.PROCESSING
    assert row.failure_code is None
    assert world.repository.publications == []
    # Lease recovery: a later run reclaims under a new fence and publishes.
    world.clock.advance(steps.policy.lease_seconds)
    recovered = EnrichmentRunner(
        world.steps(FakeArticleEnricher()), RecordingTelemetry()
    ).run(5)
    assert OutcomeKind.READY in {o.kind for o in recovered.outcomes}
    assert world.repository.job(job).status is S.READY


def test_publication_committed_first_is_not_rolled_back_by_late_cancel() -> None:
    world = MemoryWorld()
    job = ingest(world, FakeArticleEnricher())
    scope = CancellationScope()
    report = EnrichmentRunner(
        world.steps(FakeArticleEnricher()), RecordingTelemetry()
    ).run(1, scope)
    assert report.outcomes[0].kind is OutcomeKind.READY
    assert scope.cancel() is CancelAck.ALREADY_PUBLISHED
    assert world.repository.job(job).status is S.READY
    assert len(world.repository.publications) == 1


def test_cancel_recorded_before_publish_lock_returns_cancelled() -> None:
    world = MemoryWorld()
    _ = ingest(world, FakeArticleEnricher())
    steps = world.steps(FakeArticleEnricher())
    claim = steps.claim()
    assert claim is not None
    assert world.repository.request_cancel(claim) is CancelAck.ACKNOWLEDGED
    article = world.repository.documents[(claim.key.article_id, claim.key.content_hash)]
    metadata = steps.validate(
        claim,
        article,
        FakeArticleEnricher().enrich(_request(article)),
        CancellationScope(),
    )
    assert world.repository.publish(claim, 1, metadata) is PublishResult.CANCELLED  # pyright: ignore[reportArgumentType]


def test_crash_between_persist_and_enqueue_is_recovered_by_reconciliation() -> None:
    world = MemoryWorld()
    article = document(CONTENT)
    world.repository.persist(article)
    assert world.repository.jobs == {}
    report = EnrichmentRunner(
        world.steps(FakeArticleEnricher()), RecordingTelemetry()
    ).run(5)
    assert report.reconciled == 1
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]


def test_compiled_graph_isolates_invocations_and_has_no_checkpointer() -> None:
    world = MemoryWorld()
    enricher = ScriptedArticleEnricher([TIMEOUT])
    enricher.script.append(
        lambda request: grounded_draft(request.context.normalized_content)
    )
    _ = ingest(world, enricher)
    _ = ingest(world, enricher, "The Harbor Authority reopened the ferry terminal.")
    steps = world.steps(enricher)
    graph = EnrichmentGraph(steps)
    assert graph._compiled.checkpointer is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    first, second = steps.claim(), steps.claim()
    assert first is not None
    assert second is not None
    one = graph.invoke(first, CancellationScope())
    two = graph.invoke(second, CancellationScope())
    assert (one.kind, one.failure_code) == (
        OutcomeKind.RETRY_SCHEDULED,
        FailureCode.PROVIDER_TIMEOUT,
    )
    assert (two.kind, two.failure_code) == (OutcomeKind.READY, None)
    assert (one.job_id, two.job_id) == (first.job_id, second.job_id)


@pytest.mark.parametrize("max_jobs", [0, 26])
def test_pass_size_is_bounded_by_policy(max_jobs: int) -> None:
    world = MemoryWorld()
    runner = EnrichmentRunner(world.steps(FakeArticleEnricher()), RecordingTelemetry())
    with pytest.raises(InvalidPassSizeError):
        _ = runner.run(max_jobs)
    assert world.repository.calls == []


def test_runner_processes_at_most_max_jobs() -> None:
    world = MemoryWorld()
    for index in range(3):
        _ = ingest(world, FakeArticleEnricher(), f"{CONTENT} item {index}")
    runner = EnrichmentRunner(world.steps(FakeArticleEnricher()), RecordingTelemetry())
    assert len(runner.run(2).outcomes) == 2
    assert len(runner.run(2).outcomes) == 1


def _request(article: object) -> EnrichmentRequest:
    assert isinstance(article, ArticleDocument)
    key = identity_of(FakeArticleEnricher()).key_for(
        article.article_id, article.content_hash
    )
    return EnrichmentRequest(PromptContext.from_document(article), key, 30)
