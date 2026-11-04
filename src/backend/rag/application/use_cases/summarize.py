"""Article summarization steps called by the summarization workflow nodes.

``load exact version -> prepare -> summarize -> validate -> verify current
-> publish``; any typed failure goes to ``fail`` (retry with backoff for
transient provider errors, FAILED otherwise). No global retrieval is used:
the model sees only the claimed revision. A revision that stopped being
current is never published (job STALE).
"""

from dataclasses import dataclass
from uuid import UUID

from backend.rag.application.ports.summary import (
    ArticleSummaryModelPort,
    SummaryModelRequest,
    SummaryModelResponse,
    SummaryRepository,
)
from backend.rag.application.ports.work_queue import WorkQueue
from backend.rag.domain.summary import (
    CALL_TIMEOUT_SECONDS,
    MAX_INPUT_CHARS,
    MAX_OUTPUT_TOKENS,
    SUMMARY_POLICY_ID,
    SUMMARY_SCHEMA_VERSION,
    SummaryFailureCode,
    SummaryOutcome,
    SummaryProviderError,
    SummaryResult,
    SummarySource,
    SummaryStatusName,
    parse_draft,
    validate_draft,
)
from backend.rag.domain.work_jobs import (
    SUMMARY_WORK,
    ClaimedWorkJob,
    JobKind,
    WorkPolicy,
)


@dataclass(frozen=True, slots=True)
class SummaryStepFailure:
    """Typed failure routed to the ``fail`` node."""

    code: SummaryFailureCode


def _revision(job: ClaimedWorkJob) -> tuple[UUID, str]:
    return job.subject_id, job.revision_key


@dataclass(frozen=True, slots=True)
class SummarySteps:
    """Application services of the summarization workflow."""

    repository: SummaryRepository
    model: ArticleSummaryModelPort
    queue: WorkQueue
    policy: WorkPolicy = SUMMARY_WORK
    max_input_chars: int = MAX_INPUT_CHARS
    timeout_seconds: float = CALL_TIMEOUT_SECONDS

    def claim(self, worker_id: str, limit: int) -> tuple[ClaimedWorkJob, ...]:
        """Expire exhausted jobs, then claim due summary jobs."""
        _ = self.queue.expire(JobKind.SUMMARY)
        return self.queue.claim(
            JobKind.SUMMARY, worker_id, self.policy.lease_seconds, limit
        )

    def load(
        self, job: ClaimedWorkJob
    ) -> SummarySource | SummaryStepFailure | SummaryOutcome:
        """The exact claimed revision; a superseded one ends as STALE."""
        article_id, content_hash = _revision(job)
        if not self.repository.is_current(article_id, content_hash):
            return self._stale(job)
        source = self.repository.load(article_id, content_hash)
        if source is None:
            return SummaryStepFailure(SummaryFailureCode.REVISION_MISSING)
        return source

    def prepare(self, source: SummarySource) -> SummarySource | SummaryStepFailure:
        """Refuse (never truncate) content beyond the input budget."""
        if len(source.content) > self.max_input_chars:
            return SummaryStepFailure(SummaryFailureCode.CONTENT_TOO_LONG)
        return source

    def summarize(
        self, source: SummarySource
    ) -> SummaryModelResponse | SummaryStepFailure:
        """One model call over the revision; typed provider errors only."""
        try:
            return self.model.summarize(
                SummaryModelRequest(
                    source=source,
                    timeout_seconds=self.timeout_seconds,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
            )
        except SummaryProviderError as error:
            return SummaryStepFailure(error.code)

    @staticmethod
    def validate(
        source: SummarySource, response: SummaryModelResponse
    ) -> SummaryResult | SummaryStepFailure:
        """Strict draft parse and exact span binding."""
        try:
            draft = parse_draft(response.content)
            points = validate_draft(draft, source.content)
        except SummaryProviderError as error:
            return SummaryStepFailure(error.code)
        return SummaryResult(
            text=draft.summary,
            key_points=points,
            input_chars=len(source.content),
            usage=response.usage,
        )

    def verify_current(self, job: ClaimedWorkJob) -> SummaryOutcome | None:
        """Re-check the revision after the model call (``None`` = still current)."""
        article_id, content_hash = _revision(job)
        if self.repository.is_current(article_id, content_hash):
            return None
        return self._stale(job)

    def publish(
        self, job: ClaimedWorkJob, source: SummarySource, result: SummaryResult
    ) -> SummaryOutcome:
        """Fenced publication (re-checks the revision inside the transaction)."""
        status = self.repository.publish(
            fence=job.fence,
            source=source,
            identity=self.model.identity,
            policy_id=SUMMARY_POLICY_ID,
            schema_version=SUMMARY_SCHEMA_VERSION,
            result=result,
        )
        return SummaryOutcome(job.fence.job_id, job.subject_id, status)

    def fail(self, job: ClaimedWorkJob, code: SummaryFailureCode) -> SummaryOutcome:
        """Retry transient failures with backoff; everything else is FAILED."""
        if code.retryable:
            status = self.queue.retry(
                job.fence, code.value, self.policy.backoff(job.attempt)
            )
            name: SummaryStatusName = "lost"
            if status is not None:
                name = "retry" if status == "PENDING" else "failed"
            return SummaryOutcome(job.fence.job_id, job.subject_id, name, code.value)
        failed = self.queue.fail(job.fence, code.value)
        return SummaryOutcome(
            job.fence.job_id, job.subject_id, "failed" if failed else "lost", code.value
        )

    def _stale(self, job: ClaimedWorkJob) -> SummaryOutcome:
        marked = self.queue.mark(job.fence, "STALE", "revision_superseded")
        return SummaryOutcome(
            job.fence.job_id, job.subject_id, "stale" if marked else "lost"
        )
