"""Summarization workflow and conversation-run executor with in-memory fakes."""

from typing import TYPE_CHECKING, cast
from uuid import UUID

from backend.rag.application.use_cases.query_runs import QueryRunExecutor
from backend.rag.application.use_cases.summarize import SummarySteps
from backend.rag.composition import FIXTURE_CALLER
from backend.rag.domain.conversation import ArticlePin, ScopeType, conversation_scope
from backend.rag.domain.summary import MAX_INPUT_CHARS, SummaryFailureCode
from backend.rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisProviderError,
)
from backend.rag.domain.work_jobs import JobKind
from backend.rag.workflows.summarization.workflow import SummaryRunner
from briefing_support import NO_MATCH, QUESTION, harness, raising
from news_support import (
    CONTENT,
    FixedCatalog,
    MemoryQueue,
    MemoryRuns,
    MemorySummaries,
    ScriptedSummarizer,
    draft_quoting,
)

if TYPE_CHECKING:
    from backend.rag.application.ports.catalog import ArticleCatalogStore
    from backend.rag.application.ports.conversations import ConversationStore

QUOTE = "Sở Giao thông TP.HCM khuyến cáo người dân đi đường vòng"


def _summaries(
    outputs: list[str | SummaryFailureCode],
) -> tuple[SummaryRunner, MemoryQueue, MemorySummaries, ScriptedSummarizer]:
    queue = MemoryQueue()
    repo = MemorySummaries(queue)
    model = ScriptedSummarizer(outputs)
    runner = SummaryRunner(SummarySteps(repo, model, queue))
    return runner, queue, repo, model


def test_summary_is_published_for_the_exact_revision_only() -> None:
    runner, queue, repo, model = _summaries([draft_quoting(QUOTE)])
    source = repo.add()
    job = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)

    report = runner.run(5, "w")

    assert [o.status for o in report.outcomes] == ["published"]
    assert queue.jobs[job.job_id].status == "DONE"
    assert model.requests[0].source == source  # no retrieval, one revision
    _, digest, result = repo.published[0]
    assert digest == source.content_hash
    span = result.key_points[0].spans[0]
    assert CONTENT[span.start_offset : span.end_offset] == QUOTE


def test_timeout_retries_then_fails_when_attempts_run_out() -> None:
    timeout = SummaryFailureCode.PROVIDER_TIMEOUT
    runner, queue, repo, _ = _summaries([timeout, timeout, timeout])
    source = repo.add()
    job = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)

    statuses = [o.status for _ in range(3) for o in runner.run(1, "w").outcomes]

    assert statuses == ["retry", "retry", "failed"]
    assert queue.jobs[job.job_id].failure_code == "provider_timeout"
    assert repo.published == []


def test_malformed_output_fails_without_retry_and_publishes_nothing() -> None:
    runner, queue, repo, _ = _summaries(["{not json"])
    source = repo.add()
    job = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)
    assert [o.status for o in runner.run(5, "w").outcomes] == ["failed"]
    assert queue.jobs[job.job_id].failure_code == "malformed_output"
    assert repo.published == []


def test_quote_outside_the_revision_is_invalid_evidence() -> None:
    runner, queue, repo, _ = _summaries(
        [draft_quoting("Một câu không có trong bài viết")]
    )
    source = repo.add()
    job = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)
    _ = runner.run(1, "w")
    assert queue.jobs[job.job_id].failure_code == "invalid_evidence"


def test_job_of_an_old_revision_never_publishes() -> None:
    runner, queue, repo, model = _summaries([draft_quoting(QUOTE)])
    old = repo.add()
    job = queue.add(JobKind.SUMMARY, old.article_id, old.content_hash)
    repo.current[old.article_id] = "sha256:" + "f" * 64  # corrected meanwhile

    report = runner.run(1, "w")

    assert [o.status for o in report.outcomes] == ["stale"]
    assert queue.jobs[job.job_id].status == "STALE"
    assert model.requests == []  # superseded before any model call
    assert repo.published == []


def test_long_article_is_refused_not_truncated() -> None:
    runner, queue, repo, model = _summaries([])
    source = repo.add("x" * (MAX_INPUT_CHARS + 1))
    job = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)
    _ = runner.run(1, "w")
    assert queue.jobs[job.job_id].failure_code == "content_too_long"
    assert model.requests == []


# --- conversation runs ------------------------------------------------------


def _executor(
    runs: MemoryRuns,
    queue: MemoryQueue,
    catalog: FixedCatalog,
    *,
    model: bool = True,
    behaviour: object = None,
) -> QueryRunExecutor:
    h = harness(behaviour=cast("None", behaviour))
    return QueryRunExecutor(
        store=cast("ConversationStore", cast("object", runs)),
        queue=queue,
        catalog=cast("ArticleCatalogStore", cast("object", catalog)),
        briefing=h.runtime.use_case if model else None,
        caller=FIXTURE_CALLER,
    )


def _all_versions() -> dict[UUID, str]:
    h = harness()
    return {d.article_id: d.content_hash for d in h.store.documents.values()}


def _run(
    question: str, scope: dict[str, object], **kwargs: object
) -> tuple[MemoryRuns, UUID]:
    runs, queue = MemoryRuns(), MemoryQueue()
    run_id = runs.add(question, scope)
    _ = queue.add(JobKind.QUERY_RUN, run_id, "run")
    catalog = cast("FixedCatalog", kwargs.pop("catalog", FixedCatalog(_all_versions())))
    executor = _executor(runs, queue, catalog, **cast("dict[str, bool]", kwargs))
    job = executor.claim("w", 1)[0]
    _ = executor.execute(job)
    return runs, run_id


ALL = conversation_scope(ScopeType.ALL, None, ()).canonical()


def test_answered_run_records_effective_scope_and_versions() -> None:
    runs, run_id = _run(QUESTION, ALL)
    done = runs.completions[run_id]
    assert runs.status[run_id] == "completed"
    assert done.outcome.value == "answered"
    assert done.effective_scope["type"] == "all"
    assert done.source_versions
    assert all(
        c.content_hash == done.source_versions[str(c.article_id)]
        for c in done.citations
    )


def test_no_evidence_is_an_abstention_not_a_failure() -> None:
    runs, run_id = _run(NO_MATCH, ALL)
    assert runs.status[run_id] == "completed"
    assert runs.completions[run_id].outcome.value == "abstained"


def test_provider_failure_fails_the_run_instead_of_abstaining() -> None:
    error = SynthesisProviderError(ProviderFailureCode.TIMEOUT)
    runs, run_id = _run(QUESTION, ALL, behaviour=raising(error))
    assert runs.status[run_id] == "failed"
    assert runs.failures[run_id][0] == "model_unavailable"


def test_without_a_model_runs_fail_and_never_use_a_fake() -> None:
    runs, run_id = _run(QUESTION, ALL, model=False)
    assert runs.failures[run_id] == ("model_unavailable", "provider_not_configured")
    assert run_id not in runs.completions


def test_pinned_version_that_changed_fails_with_scope_stale() -> None:
    versions = _all_versions()
    article_id = next(iter(versions))
    scope = conversation_scope(
        ScopeType.ARTICLES, None, (ArticlePin(article_id, "sha256:" + "0" * 64),)
    ).canonical()
    runs, run_id = _run(QUESTION, scope, catalog=FixedCatalog(versions))
    assert runs.failures[run_id] == ("scope_stale", "pinned_version_superseded")


def test_removed_article_in_scope_fails_explicitly() -> None:
    versions = _all_versions()
    gone = next(iter(versions))
    scope = conversation_scope(
        ScopeType.ARTICLES, None, (ArticlePin(gone, None),)
    ).canonical()
    remaining = {a: h for a, h in versions.items() if a != gone}
    runs, run_id = _run(QUESTION, scope, catalog=FixedCatalog(remaining))
    assert runs.failures[run_id][0] == "scope_article_unavailable"


def test_cancel_requested_before_start_ends_cancelled_without_model_call() -> None:
    runs, queue = MemoryRuns(), MemoryQueue()
    run_id = runs.add(QUESTION, ALL)
    runs.cancel_flags.add(run_id)
    _ = queue.add(JobKind.QUERY_RUN, run_id, "run")
    executor = _executor(runs, queue, FixedCatalog(_all_versions()))
    result = executor.execute(executor.claim("w", 1)[0])
    assert result.status == "cancelled"
    assert runs.status[run_id] == "cancelled"
    assert run_id not in runs.completions
