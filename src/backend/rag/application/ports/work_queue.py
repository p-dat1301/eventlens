"""Durable work queue port (``rag_work_jobs``); every write is fenced."""

from typing import Protocol

from backend.rag.domain.work_jobs import (
    ClaimedWorkJob,
    ExpiredWorkJob,
    JobFence,
    JobKind,
)


class WorkQueue(Protocol):
    """Claim/lease/retry operations; adapters never hold a transaction open."""

    def expire(self, kind: JobKind) -> tuple[ExpiredWorkJob, ...]:
        """Fail jobs whose lease budget or deadline is exhausted."""
        ...

    def claim(
        self, kind: JobKind, worker_id: str, lease_seconds: float, limit: int
    ) -> tuple[ClaimedWorkJob, ...]:
        """Claim due jobs of one kind (``SKIP LOCKED``)."""
        ...

    def extend(self, fence: JobFence, lease_seconds: float) -> bool:
        """Heartbeat; ``False`` when ownership was lost."""
        ...

    def complete(self, fence: JobFence) -> bool:
        """Mark DONE under the fence."""
        ...

    def retry(self, fence: JobFence, code: str, delay_seconds: float) -> str | None:
        """PENDING again (or FAILED when out of attempts); new status or None."""
        ...

    def fail(self, fence: JobFence, code: str) -> bool:
        """Terminal FAILED under the fence."""
        ...

    def mark(self, fence: JobFence, status: str, code: str | None) -> bool:
        """Terminal STALE/CANCELLED under the fence."""
        ...
