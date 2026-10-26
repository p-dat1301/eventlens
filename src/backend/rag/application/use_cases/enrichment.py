"""Enrichment job steps: claim, enrich, validate, stage, publish, fail.

Every model, embedding and DB operation is admitted through the
invocation cancellation scope first (see ``CancellationScope``). No step
retries: the attempt budget, retry schedule and lease recovery are owned by
the job repository (DB orchestration) alone.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from threading import RLock

from backend.rag.application.ports.embeddings import EmbeddingPort
from backend.rag.application.ports.enrichment import (
    ArticleEnricher,
    Clock,
    EnrichmentProviderError,
    EnrichmentRepository,
    EnrichmentRequest,
)
from backend.rag.application.ports.staging import GenerationStaging
from backend.rag.application.use_cases.index import EnsureGeneration, StagedGeneration
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.embedding import EmbeddingIdentity, EmbeddingVector
from backend.rag.domain.enrichment_draft import (
    DraftRejectedError,
    EnrichmentDraft,
    PromptContext,
    validate_draft,
)
from backend.rag.domain.enrichment_lifecycle import (
    CancelAck,
    ClaimedJob,
    EnrichmentIdentity,
    FailureCode,
    JobOutcome,
    OutcomeKind,
    PublishResult,
    RetryPolicy,
)
from backend.rag.domain.staging import InvalidStageError, StageRequest
from backend.shared.contracts import ArticleEnrichment

RETRY_POLICY_RESOURCE = "enrichment-retry.v1.json"
ENRICHMENT_VERSION = "article-enrichment.v1"


def bundled_retry_policy() -> RetryPolicy:
    """Load the versioned policy shipped with the package."""
    text = (
        files("backend.rag.application")
        .joinpath("policies")
        .joinpath(RETRY_POLICY_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return RetryPolicy.model_validate_json(text)


class InvocationCancelledError(RuntimeError):
    """Cancellation was acknowledged before this operation was admitted."""

    def __init__(self) -> None:
        """Fixed message; cancellation is not a provider failure."""
        super().__init__("enrichment invocation cancelled")


class CancellationScope:
    """Per-run cancellation with an explicit admission order.

    Every model, embedding or DB operation of a run passes ``admit`` (reads,
    model and embedding calls) or ``serialized`` (job/stage writes) first. Both
    take the scope lock, which ``cancel`` also holds while it sets ``stopped``
    and records the request in the job row. Therefore an operation either was
    admitted before the acknowledgement or never starts; ``serialized`` writes
    finish (commit) before a concurrent ``cancel`` can acknowledge. Publication
    is admitted, then linearized against ``cancel`` by the job row lock inside
    the publish transaction: an acknowledged cancel blocks publication, an
    earlier publication stays valid and the cancel reports ``ALREADY_PUBLISHED``.
    """

    def __init__(self) -> None:
        """Start unbound and not stopped."""
        self._lock: RLock = RLock()
        self._binding: tuple[EnrichmentRepository, ClaimedJob] | None = None
        self._stopped: bool = False
        self._ack: CancelAck | None = None

    def bind(self, repository: EnrichmentRepository, claim: ClaimedJob) -> None:
        """Attach the job currently owned by this run."""
        with self._lock:
            self._binding = (repository, claim)

    @property
    def stopped(self) -> bool:
        """Whether no further work may start."""
        return self._stopped

    @property
    def acknowledgement(self) -> CancelAck | None:
        """Repository verdict for the bound job, if a cancel happened."""
        return self._ack

    def admit(self) -> None:
        """Admit one read/model/embedding operation or raise if cancelled."""
        with self._lock:
            if self._stopped:
                raise InvocationCancelledError

    def serialized[T](self, operation: Callable[[], T]) -> T:
        """Run one write while holding the scope lock, unless cancelled."""
        with self._lock:
            if self._stopped:
                raise InvocationCancelledError
            return operation()

    def cancel(self) -> CancelAck | None:
        """Stop the run; record a fenced cancellation for the bound job."""
        with self._lock:
            self._stopped = True
            if self._binding is None:
                return None
            repository, claim = self._binding
            self._ack = repository.request_cancel(claim)
            return self._ack


@dataclass(frozen=True, slots=True)
class _AdmittedStaging:
    """GenerationStaging whose lookup is admitted and whose write is serialized."""

    inner: GenerationStaging
    scope: CancellationScope

    def find_complete(
        self, article: ArticleDocument, identity: EmbeddingIdentity
    ) -> int | None:
        self.scope.admit()
        return self.inner.find_complete(article, identity)

    def stage(self, request: StageRequest) -> int:
        return self.scope.serialized(lambda: self.inner.stage(request))


@dataclass(frozen=True, slots=True)
class _AdmittedEmbeddings:
    """EmbeddingPort admitting every embedding call separately."""

    inner: EmbeddingPort
    scope: CancellationScope

    def embed(self, text: str) -> EmbeddingVector:
        self.scope.admit()
        return self.inner.embed(text)


@dataclass(frozen=True, slots=True)
class StepFailure:
    """Typed failure routed to the fenced retry/failure step."""

    code: FailureCode


@dataclass(frozen=True, slots=True)
class EnrichmentSteps:
    """Framework-free services invoked by enrichment graph nodes."""

    repository: EnrichmentRepository
    enricher: ArticleEnricher
    generations: EnsureGeneration
    policy: RetryPolicy
    clock: Clock

    @property
    def identity(self) -> EnrichmentIdentity:
        """Identity this worker produces, taken from its enricher."""
        return EnrichmentIdentity(
            enrichment_version=ENRICHMENT_VERSION,
            model_version=self.enricher.model_version,
            prompt_version=self.enricher.prompt_version,
        )

    def claim(self) -> ClaimedJob | None:
        """Claim one eligible job for this worker's exact identity."""
        return self.repository.claim_next(self.identity, self.policy)

    def claim_into(self, scope: CancellationScope) -> ClaimedJob | None:
        """Serialized claim that binds the job to the scope atomically."""

        def claim_and_bind() -> ClaimedJob | None:
            claim = self.claim()
            if claim is not None:
                scope.bind(self.repository, claim)
            return claim

        try:
            return scope.serialized(claim_and_bind)
        except InvocationCancelledError:
            return None

    def reconcile(self, limit: int) -> int:
        """Enqueue jobs for current articles persisted without one."""
        return self.repository.enqueue_missing(self.identity, limit)

    def load(
        self, claim: ClaimedJob, scope: CancellationScope
    ) -> ArticleDocument | JobOutcome:
        """Load the claimed revision, or fence it STALE when superseded."""
        try:
            scope.admit()
            article = self.repository.load_current(claim)
            if article is not None:
                return article
            stale = scope.serialized(lambda: self.repository.mark_stale(claim))
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED)
        return _outcome(
            claim, OutcomeKind.STALE if stale else OutcomeKind.LOST_OWNERSHIP
        )

    def enrich(
        self, claim: ClaimedJob, article: ArticleDocument, scope: CancellationScope
    ) -> EnrichmentDraft | StepFailure | JobOutcome:
        """Call the provider exactly once with allowlisted context."""
        request = EnrichmentRequest(
            context=PromptContext.from_document(article),
            key=claim.key,
            deadline_seconds=self.policy.attempt_timeout_seconds,
        )
        try:
            scope.admit()
            return self.enricher.enrich(request)
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED)
        except EnrichmentProviderError as error:
            return StepFailure(error.code)

    def validate(
        self,
        claim: ClaimedJob,
        article: ArticleDocument,
        draft: EnrichmentDraft,
        scope: CancellationScope,
    ) -> ArticleEnrichment | StepFailure | JobOutcome:
        """Bind the draft to job identity and exact content."""
        try:
            scope.admit()
            return validate_draft(article, claim, draft, self.clock()).metadata
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED)
        except DraftRejectedError as error:
            return StepFailure(error.code)

    def stage(
        self, claim: ClaimedJob, article: ArticleDocument, scope: CancellationScope
    ) -> StagedGeneration | StepFailure | JobOutcome:
        """Reuse or stage a generation; lookup, each embedding and write admitted."""
        admitted = EnsureGeneration(
            _AdmittedStaging(self.generations.staging, scope),
            _AdmittedEmbeddings(self.generations.embeddings, scope),
            self.generations.identity,
        )
        try:
            return admitted.execute(article)
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED)
        except InvalidStageError:
            return StepFailure(FailureCode.INVALID_STAGE)

    def publish(
        self,
        claim: ClaimedJob,
        staged: StagedGeneration,
        metadata: ArticleEnrichment,
        scope: CancellationScope,
    ) -> JobOutcome:
        """Final admission check, then one fenced atomic publication."""
        try:
            scope.admit()
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED)
        result = self.repository.publish(claim, staged.generation, metadata)
        kind = {
            PublishResult.PUBLISHED: OutcomeKind.READY,
            PublishResult.CANCELLED: OutcomeKind.CANCELLED,
            PublishResult.LOST_OWNERSHIP: OutcomeKind.LOST_OWNERSHIP,
        }[result]
        return _outcome(
            claim,
            kind,
            index_generation=staged.generation,
            embedding_reused=staged.reused,
        )

    def fail(
        self, claim: ClaimedJob, code: FailureCode, scope: CancellationScope
    ) -> JobOutcome:
        """Apply the versioned policy through one serialized fenced write."""
        decision = self.policy.decide(claim.attempt, code)

        def write() -> bool:
            if decision.retry_in_seconds is not None:
                return self.repository.schedule_retry(
                    claim, code, decision.retry_in_seconds
                )
            return self.repository.mark_failed(claim, code)

        try:
            written = scope.serialized(write)
        except InvocationCancelledError:
            return _outcome(claim, OutcomeKind.CANCELLED, failure_code=code)
        if not written:
            return _outcome(claim, OutcomeKind.LOST_OWNERSHIP, failure_code=code)
        kind = (
            OutcomeKind.RETRY_SCHEDULED
            if decision.retry_in_seconds is not None
            else OutcomeKind.FAILED
        )
        return _outcome(
            claim, kind, failure_code=code, retry_in_seconds=decision.retry_in_seconds
        )


def _outcome(  # noqa: PLR0913
    claim: ClaimedJob,
    kind: OutcomeKind,
    *,
    failure_code: FailureCode | None = None,
    retry_in_seconds: int | None = None,
    index_generation: int | None = None,
    embedding_reused: bool | None = None,
) -> JobOutcome:
    return JobOutcome(
        job_id=claim.job_id,
        article_id=claim.key.article_id,
        claim_generation=claim.claim_generation,
        attempt=claim.attempt,
        kind=kind,
        failure_code=failure_code,
        retry_in_seconds=retry_in_seconds,
        index_generation=index_generation,
        embedding_reused=embedding_reused,
    )
