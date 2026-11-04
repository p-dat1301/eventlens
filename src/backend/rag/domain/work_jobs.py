"""Durable PostgreSQL work jobs (``rag_work_jobs``) for summaries and chat runs.

A job is claimed with ``FOR UPDATE SKIP LOCKED``; every claim bumps
``claim_generation`` and counts one attempt (an expired lease therefore costs
an attempt too). Every later write matches the fence ``(job_id,
claim_generation, PROCESSING, lease not expired)``; zero rows means another
worker owns the job now and the result is discarded. Each kind has its own
claim, so summary work can never take the slots reserved for chat runs.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class JobKind(StrEnum):
    """Queues with separately sized worker pools."""

    SUMMARY = "summary"
    QUERY_RUN = "query_run"


@dataclass(frozen=True, slots=True)
class JobFence:
    """Ownership proof of one claim."""

    job_id: UUID
    claim_generation: int


@dataclass(frozen=True, slots=True)
class ClaimedWorkJob:
    """One claimed job and its attempt position."""

    fence: JobFence
    kind: JobKind
    subject_id: UUID
    revision_key: str
    attempt: int
    max_attempts: int
    deadline_at: datetime | None

    @property
    def last_attempt(self) -> bool:
        """No retry remains after this attempt."""
        return self.attempt >= self.max_attempts


@dataclass(frozen=True, slots=True)
class ExpiredWorkJob:
    """A job failed by the sweep (lease budget or deadline exhausted)."""

    job_id: UUID
    subject_id: UUID
    failure_code: str


@dataclass(frozen=True, slots=True)
class WorkPolicy:
    """Lease and backoff of one queue."""

    lease_seconds: float
    backoff_seconds: tuple[float, ...]

    def backoff(self, attempt: int) -> float:
        """Delay before attempt ``attempt + 1`` (last value repeats)."""
        index = min(max(attempt - 1, 0), len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index]


SUMMARY_WORK = WorkPolicy(lease_seconds=180.0, backoff_seconds=(30.0, 120.0))
QUERY_WORK = WorkPolicy(lease_seconds=60.0, backoff_seconds=(2.0,))
