"""Worker-side execution of durable conversation runs.

One claimed ``query_run`` job = one invocation of the compiled briefing graph
(the LangGraph query workflow) with a server-resolved article restriction:

``start run -> resolve scope (catalog semantics, current revisions) ->
standalone retrieval query from previous *questions* -> briefing graph
(retrieve in scope -> final current/scope check -> synthesize -> validate)
-> store answer + citations + effective scope atomically``.

Model or storage failures fail the run with a stable code; only an empty
evidence set or a model abstention completes it as ``abstained``. A cancel
request is polled while the graph runs and stops it cooperatively.
"""

# ruff: noqa: EM101

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import ValidationError

from backend.rag.application.ports.catalog import ArticleCatalogStore
from backend.rag.application.ports.conversations import ConversationStore
from backend.rag.application.ports.work_queue import WorkQueue
from backend.rag.application.use_cases.briefing import BriefingOutcome, BriefingUseCase
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_scope import ArticleRestriction
from backend.rag.domain.catalog import CatalogRequestError, CatalogStorageError
from backend.rag.domain.conversation import (
    HISTORY_POLICY_ID,
    MAX_FILTERED_SCOPE,
    RunCompletion,
    RunOutcome,
    RunWork,
    ScopeInvalidError,
    ScopeType,
    StoredCitation,
    retrieval_query,
    scope_from_canonical,
)
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import (
    RetrievalAccessDeniedError,
    RetrievalCancelledError,
    RetrievalConfigurationError,
    RetrievalDeadlineExceededError,
    RetrievalStorageError,
)
from backend.rag.domain.synthesis_errors import (
    ProviderActivationError,
    SynthesisAccessDeniedError,
    SynthesisBudgetError,
    SynthesisCancelledError,
    SynthesisConcurrencyRejectedError,
    SynthesisConfigurationError,
    SynthesisDeadlineExceededError,
    SynthesisEvidenceStaleError,
    SynthesisGroundingError,
    SynthesisMalformedOutputError,
    SynthesisProviderError,
    SynthesisStorageError,
)
from backend.rag.domain.work_jobs import QUERY_WORK, ClaimedWorkJob, JobKind, WorkPolicy

RESULT_LIMIT: Final = 5
CONTEXT_BUDGET: Final = 12_000
CANCEL_POLL_SECONDS: Final = 0.5

# Stable run failure codes (same vocabulary as the briefing error table).
RUN_FAILURES: Final[tuple[tuple[tuple[type[Exception], ...], str], ...]] = (
    ((RetrievalAccessDeniedError, SynthesisAccessDeniedError), "access_denied"),
    (
        (RetrievalDeadlineExceededError, SynthesisDeadlineExceededError),
        "deadline_exceeded",
    ),
    ((SynthesisEvidenceStaleError,), "evidence_stale"),
    ((SynthesisBudgetError,), "budget_exceeded"),
    ((SynthesisConcurrencyRejectedError,), "capacity_exceeded"),
    (
        (SynthesisMalformedOutputError, SynthesisGroundingError),
        "invalid_grounded_answer",
    ),
    ((RetrievalStorageError, SynthesisStorageError), "retrieval_unavailable"),
    ((SynthesisProviderError,), "model_unavailable"),
    (
        (
            RetrievalConfigurationError,
            SynthesisConfigurationError,
            ProviderActivationError,
        ),
        "configuration_invalid",
    ),
)


def failure_code(error: Exception) -> tuple[str, str | None]:
    """Stable code and enum reason; messages are never stored."""
    reason: object = getattr(error, "reason", None)
    for kinds, code in RUN_FAILURES:
        if isinstance(error, kinds):
            return code, None if reason is None else str(reason)
    return "internal_error", None


class RunScopeError(Exception):
    """The stored scope cannot be applied to this run."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        """Stable code plus optional enum reason."""
        super().__init__(code)
        self.code: str = code
        self.reason: str | None = reason


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """Restriction plus its public effective-scope record."""

    restriction: ArticleRestriction | None
    effective: dict[str, object]
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RunResult:
    """Terminal result of one claimed run job."""

    run_id: UUID
    status: str
    code: str | None = None


def _answer(outcome: BriefingOutcome) -> dict[str, object]:
    result = outcome.result
    answer = result.answer
    provenance = result.provenance
    rprov = outcome.retrieval.provenance
    return {
        "answer": answer.answer,
        "claims": [
            {
                "claim_id": str(c.claim_id),
                "text": c.text,
                "temporal_claim": None
                if c.temporal_claim is None
                else c.temporal_claim.model_dump(mode="json"),
            }
            for c in answer.claims
        ],
        "uncertainties": [
            {
                "uncertainty_id": str(u.uncertainty_id),
                "category": u.category,
                "explanation": u.explanation,
                "claim_id": None if u.claim_id is None else str(u.claim_id),
            }
            for u in answer.uncertainties
        ],
        "retrieved_at": answer.retrieved_at.isoformat(),
        "model_version": answer.model_version,
        "mode": provenance.mode,
        "retrieval_mode": outcome.retrieval.retrieval_mode.value,
        "audit": {
            "retrieval_policy_id": rprov.policy_id,
            "fusion_policy_id": rprov.fusion_policy_id,
            "synthesis_policy_id": provenance.synthesis_policy_id,
            "prompt_version": provenance.prompt_version,
            "draft_schema_version": provenance.draft_schema_version,
            "history_policy_id": HISTORY_POLICY_ID,
        },
    }


def _citations(outcome: BriefingOutcome) -> tuple[StoredCitation, ...]:
    return tuple(
        StoredCitation(
            citation_id=source.citation.citation_id,
            ordinal=ordinal,
            claim_id=source.citation.claim_id,
            article_id=source.span.article_id,
            content_hash=source.span.content_hash,
            span_id=source.span.span_id,
            start_offset=source.span.start_offset,
            end_offset=source.span.end_offset,
            excerpt=source.span.excerpt,
            span_fingerprint=source.span.span_fingerprint,
            source_title=source.citation.source_title,
            source_name=source.source_name,
            source_domain=source.source_domain,
            source_url=str(source.citation.source_reference),
            source_published_at=source.source_published_at,
        )
        for ordinal, source in enumerate(outcome.cited_sources)
    )


@dataclass(frozen=True, slots=True)
class QueryRunExecutor:
    """Claims and executes ``query_run`` jobs; one invocation per job."""

    store: ConversationStore
    queue: WorkQueue
    catalog: ArticleCatalogStore
    briefing: BriefingUseCase | None
    caller: CallerScope
    unavailable_reason: str = "provider_not_configured"
    policy: WorkPolicy = QUERY_WORK
    poll_seconds: float = CANCEL_POLL_SECONDS
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def claim(self, worker_id: str, limit: int) -> tuple[ClaimedWorkJob, ...]:
        """Settle expired/orphaned runs atomically, then claim due run jobs."""
        _ = self.store.reconcile_runs()
        return self.queue.claim(
            JobKind.QUERY_RUN, worker_id, self.policy.lease_seconds, limit
        )

    def resolve(self, work: RunWork) -> ResolvedScope:  # noqa: C901 - one branch per scope type
        """Re-resolve the stored scope definition against current revisions."""
        try:
            scope = scope_from_canonical(work.scope)
        except ScopeInvalidError as error:
            raise RunScopeError("invalid_scope", error.reason) from None
        effective: dict[str, object] = {
            "type": scope.scope_type.value,
            "definition_sha256": scope.digest(),
        }
        try:
            match scope.scope_type:
                case ScopeType.ALL:
                    return ResolvedScope(
                        None, effective | {"resolved_articles": None}, self.clock()
                    )
                case ScopeType.FILTERED:
                    assert scope.filters is not None  # noqa: S101 - validated shape
                    snapshot = self.catalog.resolve_scope(
                        scope.filters, MAX_FILTERED_SCOPE
                    )
                    if snapshot.total > MAX_FILTERED_SCOPE:
                        raise RunScopeError("scope_too_large")
                    return ResolvedScope(
                        ArticleRestriction(snapshot.versions),
                        effective
                        | {
                            "filters": scope.filters.canonical(),
                            "resolved_articles": snapshot.total,
                        },
                        snapshot.as_of,
                    )
                case ScopeType.ARTICLES:
                    ids = tuple(p.article_id for p in scope.articles)
                    current = self.catalog.current_versions(ids)
                    if set(current) != set(ids):
                        raise RunScopeError("scope_article_unavailable")
                    for pin in scope.articles:
                        pinned = pin.article_version_id
                        if pinned is not None and pinned != current[pin.article_id]:
                            raise RunScopeError(
                                "scope_stale", "pinned_version_superseded"
                            )
                    return ResolvedScope(
                        ArticleRestriction(current),
                        effective
                        | {
                            "resolved_articles": len(current),
                            "articles": [
                                {"article_id": str(a), "article_version_id": h}
                                for a, h in sorted(
                                    current.items(), key=lambda x: str(x[0])
                                )
                            ],
                        },
                        self.clock(),
                    )
        except CatalogStorageError:
            raise RunScopeError("retrieval_unavailable") from None
        except CatalogRequestError as error:
            raise RunScopeError("invalid_scope", error.code) from None

    def execute(self, job: ClaimedWorkJob) -> RunResult:
        """Run one claimed job to a terminal state.

        An exception that escapes (a storage outage in ``start``/``complete``/
        ``fail``) releases the claim with a backoff (one attempt consumed)
        before propagating to the supervised worker loop, so the run is
        retried instead of waiting for the lease; when attempts are spent the
        job is FAILED and ``reconcile_runs`` settles the run.
        """
        try:
            return self._execute(job)
        except Exception:
            with suppress(Exception):  # best effort: storage may still be down
                _ = self.queue.retry(
                    job.fence, "storage_unavailable", self.policy.backoff(job.attempt)
                )
            raise

    def _execute(self, job: ClaimedWorkJob) -> RunResult:  # noqa: PLR0911 - terminal states
        run_id = job.subject_id
        work = self.store.start(run_id)
        if work is None:  # cancelled, deleted or already terminal
            _ = self.store.finish_cancelled(job.fence, run_id)
            return RunResult(run_id, "cancelled")
        if self.briefing is None:
            return self._fail(job, "model_unavailable", self.unavailable_reason)
        try:
            resolved = self.resolve(work)
        except RunScopeError as error:
            return self._fail(job, error.code, error.reason)
        text = retrieval_query(work.question, work.previous_questions)
        try:
            query = BriefingQuery.model_validate(
                {
                    "schema_version": "briefing_query.v1",
                    "query": text,
                    "publication_time_basis": "source_published_at",
                    "result_limit": RESULT_LIMIT,
                    "context_budget": CONTEXT_BUDGET,
                }
            )
        except ValidationError:
            return self._fail(job, "invalid_question", None)
        try:
            outcome = self._run_graph(self.briefing, run_id, query, resolved)
        except (RetrievalCancelledError, SynthesisCancelledError):
            _ = self.store.finish_cancelled(job.fence, run_id)
            return RunResult(run_id, "cancelled")
        except Exception as error:  # noqa: BLE001 - every failure is recorded as code
            code, reason = failure_code(error)
            return self._fail(job, code, reason)
        status = self.store.complete(
            job.fence, run_id, self._completion(work, text, resolved, outcome)
        )
        return RunResult(run_id, status)

    def _run_graph(
        self,
        briefing: BriefingUseCase,
        run_id: UUID,
        query: BriefingQuery,
        resolved: ResolvedScope,
    ) -> BriefingOutcome:
        control = briefing.new_control()
        done = threading.Event()

        def watch() -> None:
            while not done.wait(self.poll_seconds):
                try:
                    if self.store.cancel_requested(run_id):
                        control.cancel()
                        return
                except Exception:  # noqa: BLE001, S112 - a failed poll never cancels
                    continue

        watcher = threading.Thread(
            target=watch, name=f"run-cancel-{run_id}", daemon=True
        )
        watcher.start()
        try:
            return briefing.execute(
                query, caller=self.caller, control=control, scope=resolved.restriction
            )
        finally:
            done.set()
            watcher.join(timeout=self.poll_seconds * 4)

    def _completion(
        self,
        work: RunWork,
        text: str,
        resolved: ResolvedScope,
        outcome: BriefingOutcome,
    ) -> RunCompletion:
        result = outcome.result
        answered = result.outcome.value == "answered"
        usage = result.usage
        return RunCompletion(
            outcome=RunOutcome.ANSWERED if answered else RunOutcome.ABSTAINED,
            abstention_reason=None
            if answered
            else (result.abstention_reason or result.outcome.value),
            answer=_answer(outcome),
            assistant_text=result.answer.answer,
            citations=_citations(outcome),
            effective_scope=resolved.effective,
            as_of=resolved.as_of,
            source_versions={
                str(a.article_id): a.content_hash for a in outcome.retrieval.articles
            },
            usage={
                "model_calls": usage.model_calls,
                "input_tokens_charged": usage.input_tokens_charged,
                "output_tokens_charged": usage.output_tokens_charged,
                "usage_status": usage.usage_status.value,
            },
            retrieval_query=text,
            history_message_ids=work.previous_message_ids
            if text != work.question
            else (),
        )

    def _fail(self, job: ClaimedWorkJob, code: str, reason: str | None) -> RunResult:
        _ = self.store.fail(job.fence, job.subject_id, code, reason)
        return RunResult(job.subject_id, "failed", code)
