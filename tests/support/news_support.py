"""In-memory fakes for the news backend (summaries, work queue, runs)."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from backend.rag.application.ports.catalog import ScopeSnapshot
from backend.rag.application.ports.summary import (
    PublishResult,
    SummaryModelRequest,
    SummaryModelResponse,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.catalog import CatalogFilters
from backend.rag.domain.conversation import RunCompletion, RunWork
from backend.rag.domain.summary import (
    SummaryFailureCode,
    SummaryModelIdentity,
    SummaryProviderError,
    SummaryResult,
    SummarySource,
)
from backend.rag.domain.work_jobs import (
    ClaimedWorkJob,
    ExpiredWorkJob,
    JobFence,
    JobKind,
)

CONTENT = (
    "Cầu Phú Mỹ tạm đóng một làn từ 22 giờ ngày 28/10. "
    "Sở Giao thông TP.HCM khuyến cáo người dân đi đường vòng qua cầu Sài Gòn."
)

SCRIPTED = SummaryModelIdentity("scripted", "v1", "p1")


def draft_quoting(*quotes: str) -> str:
    """Valid ``article_summary_draft.v1`` quoting ``quotes`` verbatim."""
    return json.dumps(
        {
            "summary": "Cầu Phú Mỹ đóng một làn.",
            "key_points": [
                {"text": q[:40], "evidence": [{"quote": q}]} for q in quotes
            ],
        },
        ensure_ascii=False,
    )


@dataclass(slots=True)
class ScriptedSummarizer:
    """Returns scripted content or raises scripted provider errors."""

    outputs: list[str | SummaryFailureCode]
    requests: list[SummaryModelRequest] = field(
        default_factory=list[SummaryModelRequest]
    )
    identity: SummaryModelIdentity = SCRIPTED

    def summarize(self, request: SummaryModelRequest) -> SummaryModelResponse:
        """Pop the next scripted behaviour."""
        self.requests.append(request)
        item = self.outputs.pop(0)
        if isinstance(item, SummaryFailureCode):
            raise SummaryProviderError(item)
        return SummaryModelResponse(item, {"input_tokens": 10}, "scripted")


@dataclass(slots=True)
class MemoryJob:
    """One in-memory job row."""

    job_id: UUID
    kind: JobKind
    subject_id: UUID
    revision_key: str
    status: str = "PENDING"
    attempts: int = 0
    max_attempts: int = 3
    generation: int = 0
    failure_code: str | None = None


@dataclass(slots=True)
class MemoryQueue:
    """``WorkQueue`` fake with the same fence rule (job + generation)."""

    jobs: dict[UUID, MemoryJob] = field(default_factory=dict[UUID, MemoryJob])

    def add(self, kind: JobKind, subject: UUID, revision: str) -> MemoryJob:
        """Enqueue."""
        job = MemoryJob(uuid4(), kind, subject, revision)
        self.jobs[job.job_id] = job
        return job

    def owned(self, fence: JobFence) -> MemoryJob | None:
        """Job when ``fence`` still holds."""
        job = self.jobs.get(fence.job_id)
        if job is None or job.status != "PROCESSING":
            return None
        return job if job.generation == fence.claim_generation else None

    def expire(self, kind: JobKind) -> tuple[ExpiredWorkJob, ...]:
        """Nothing expires in memory."""
        del kind
        return ()

    def claim(
        self, kind: JobKind, worker_id: str, lease_seconds: float, limit: int
    ) -> tuple[ClaimedWorkJob, ...]:
        """Claim PENDING jobs of ``kind``."""
        del worker_id, lease_seconds
        claimed: list[ClaimedWorkJob] = []
        for job in self.jobs.values():
            if len(claimed) >= limit:
                break
            if job.kind is kind and job.status == "PENDING":
                job.status, job.attempts = "PROCESSING", job.attempts + 1
                job.generation += 1
                claimed.append(
                    ClaimedWorkJob(
                        JobFence(job.job_id, job.generation),
                        kind,
                        job.subject_id,
                        job.revision_key,
                        job.attempts,
                        job.max_attempts,
                        None,
                    )
                )
        return tuple(claimed)

    def extend(self, fence: JobFence, lease_seconds: float) -> bool:
        """Heartbeat."""
        del lease_seconds
        return self.owned(fence) is not None

    def complete(self, fence: JobFence) -> bool:
        """DONE."""
        return self._set(fence, "DONE", None)

    def retry(self, fence: JobFence, code: str, delay_seconds: float) -> str | None:
        """PENDING again unless out of attempts."""
        del delay_seconds
        job = self.owned(fence)
        if job is None:
            return None
        job.status = "PENDING" if job.attempts < job.max_attempts else "FAILED"
        job.failure_code = code
        return job.status

    def fail(self, fence: JobFence, code: str) -> bool:
        """FAILED."""
        return self._set(fence, "FAILED", code)

    def mark(self, fence: JobFence, status: str, code: str | None) -> bool:
        """STALE / CANCELLED."""
        return self._set(fence, status, code)

    def _set(self, fence: JobFence, status: str, code: str | None) -> bool:
        job = self.owned(fence)
        if job is None:
            return False
        job.status, job.failure_code = status, code
        return True


@dataclass(slots=True)
class MemorySummaries:
    """``SummaryRepository`` fake over one revision map."""

    queue: MemoryQueue
    current: dict[UUID, str] = field(default_factory=dict[UUID, str])
    sources: dict[tuple[UUID, str], SummarySource] = field(
        default_factory=dict[tuple[UUID, str], SummarySource]
    )
    published: list[tuple[UUID, str, SummaryResult]] = field(
        default_factory=list[tuple[UUID, str, SummaryResult]]
    )

    def add(self, content: str = CONTENT) -> SummarySource:
        """Store and make current one revision."""
        article_id = uuid4()
        digest = "sha256:" + format(abs(hash(content)), "064x")[:64]
        source = SummarySource(article_id, digest, "Tiêu đề", content, "Nguồn", None)
        self.sources[(article_id, digest)] = source
        self.current[article_id] = digest
        return source

    def load(self, article_id: UUID, content_hash: str) -> SummarySource | None:
        """Exact revision."""
        return self.sources.get((article_id, content_hash))

    def is_current(self, article_id: UUID, content_hash: str) -> bool:
        """Current pointer check."""
        return self.current.get(article_id) == content_hash

    def publish(  # noqa: PLR0913 - mirrors the port
        self,
        *,
        fence: JobFence,
        source: SummarySource,
        identity: SummaryModelIdentity,
        policy_id: str,
        schema_version: str,
        result: SummaryResult,
    ) -> PublishResult:
        """Fenced, version-checked publication."""
        del identity, policy_id, schema_version
        if self.queue.owned(fence) is None:
            return "lost"
        if not self.is_current(source.article_id, source.content_hash):
            _ = self.queue.mark(fence, "STALE", "revision_superseded")
            return "stale"
        self.published.append((source.article_id, source.content_hash, result))
        _ = self.queue.complete(fence)
        return "published"


@dataclass(slots=True)
class MemoryRuns:
    """Worker-side subset of ``ConversationStore`` for executor tests."""

    works: dict[UUID, RunWork] = field(default_factory=dict[UUID, RunWork])
    status: dict[UUID, str] = field(default_factory=dict[UUID, str])
    failures: dict[UUID, tuple[str, str | None]] = field(
        default_factory=dict[UUID, tuple[str, str | None]]
    )
    completions: dict[UUID, RunCompletion] = field(
        default_factory=dict[UUID, RunCompletion]
    )
    cancel_flags: set[UUID] = field(default_factory=set[UUID])

    def add(self, question: str, scope: dict[str, object]) -> UUID:
        """One queued run."""
        run_id = uuid4()
        self.works[run_id] = RunWork(run_id, uuid4(), uuid4(), question, scope, (), ())
        self.status[run_id] = "queued"
        return run_id

    def start(self, run_id: UUID) -> RunWork | None:
        """queued/running -> running unless cancelled."""
        if self.status.get(run_id) not in {"queued", "running"} or (
            run_id in self.cancel_flags
        ):
            return None
        self.status[run_id] = "running"
        return self.works[run_id]

    def reconcile_runs(self) -> int:
        """Nothing expires in memory."""
        return 0

    def cancel_requested(self, run_id: UUID) -> bool:
        """Flag."""
        return run_id in self.cancel_flags

    def complete(self, fence: JobFence, run_id: UUID, done: RunCompletion) -> str:
        """Completed unless flagged."""
        del fence
        if run_id in self.cancel_flags:
            self.status[run_id] = "cancelled"
            return "cancelled"
        self.status[run_id] = "completed"
        self.completions[run_id] = done
        return "completed"

    def fail(
        self, fence: JobFence | None, run_id: UUID, code: str, reason: str | None
    ) -> bool:
        """Failed."""
        del fence
        self.status[run_id] = "failed"
        self.failures[run_id] = (code, reason)
        return True

    def finish_cancelled(self, fence: JobFence, run_id: UUID) -> bool:
        """Cancelled."""
        del fence
        self.status[run_id] = "cancelled"
        return True


@dataclass(slots=True)
class FixedCatalog:
    """``ArticleCatalogStore`` subset used by scope resolution."""

    versions: dict[UUID, str]

    def resolve_scope(self, filters: CatalogFilters, cap: int) -> ScopeSnapshot:
        """Every known article matches (filters are the store's job)."""
        del filters, cap
        return ScopeSnapshot(dict(self.versions), len(self.versions), datetime.now(UTC))

    def current_versions(self, article_ids: tuple[UUID, ...]) -> dict[UUID, str]:
        """Known articles only."""
        return {a: self.versions[a] for a in article_ids if a in self.versions}


def news_document(
    content: str, *, article_id: UUID | None = None, title: str = "Tin thử nghiệm"
) -> ArticleDocument:
    """Current-style ``ArticleDocument`` (publisher time 2026-10-28T17:00Z)."""
    digest = f"sha256:{sha256(content.encode()).hexdigest()}"
    return ArticleDocument.model_validate_json(
        json.dumps(
            {
                "schema_version": "article_document.v1",
                "article_id": str(article_id or uuid4()),
                "source_id": "11111111-1111-4111-8111-111111111111",
                "title": title,
                "raw_content": content,
                "normalization_version": "nfc.v1",
                "normalized_content": content,
                "canonical_url": f"https://news.example/{uuid4().hex[:8]}",
                "source_name": "Báo A",
                "source_domain": "a.example",
                "source_published_at": "2026-10-28T17:00:00+00:00",
                "ingested_at": "2026-10-29T00:00:00+00:00",
                "processed_at": "2026-10-29T00:00:00+00:00",
                "content_hash": digest,
                "content_fingerprint": digest,
                "visibility": "internal",
                "evidence_spans": [],
            }
        )
    )
