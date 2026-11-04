"""Session and conversation persistence ports.

Every owner-facing read/write takes the verified ``owner_id`` and matches it
in SQL, so a foreign conversation or run is indistinguishable from a missing
one. Adapters raise ``ConversationStorageError`` on storage failures.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.rag.domain.conversation import (
    Conversation,
    Message,
    QueryRun,
    RunCompletion,
    RunWork,
    SessionPrincipal,
    StoredCitation,
)
from backend.rag.domain.work_jobs import JobFence


class SessionStore(Protocol):
    """Server-side sessions keyed by the SHA-256 of a random token."""

    def create(self, token_sha256: str, owner_id: UUID, ttl_seconds: int) -> datetime:
        """Persist a session; returns its expiry."""
        ...

    def resolve(self, token_sha256: str) -> SessionPrincipal | None:
        """Unexpired, unrevoked session or ``None``."""
        ...

    def revoke(self, token_sha256: str) -> None:
        """Revoke (idempotent)."""
        ...


@dataclass(frozen=True, slots=True)
class NewRun:
    """Inputs of one submitted question."""

    run_id: UUID
    user_message_id: UUID
    job_id: UUID
    question: str
    idempotency_key: str | None
    request_sha256: str
    max_attempts: int
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """The run (new or replayed) and whether it was created now."""

    run: QueryRun
    created: bool


class ConversationStore(Protocol):
    """Conversations, messages, runs and citations."""

    def create(
        self,
        owner_id: UUID,
        conversation_id: UUID,
        title: str | None,
        scope: dict[str, object],
        scope_sha256: str,
    ) -> Conversation:
        """Insert a conversation."""
        ...

    def list(
        self,
        owner_id: UUID,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> tuple[Conversation, ...]:
        """Owner's conversations, newest first (keyset ``before``)."""
        ...

    def get(self, owner_id: UUID, conversation_id: UUID) -> Conversation | None:
        """Owned, not deleted conversation."""
        ...

    def delete(self, owner_id: UUID, conversation_id: UUID) -> bool:
        """Soft delete; cancels queued/running runs and their jobs atomically."""
        ...

    def messages(
        self, owner_id: UUID, conversation_id: UUID, after_sequence: int, limit: int
    ) -> tuple[Message, ...]:
        """Messages in sequence order."""
        ...

    def submit(
        self, owner_id: UUID, conversation_id: UUID, new: NewRun
    ) -> SubmitResult:
        """Message + run + job in one transaction (idempotent, one active run)."""
        ...

    def run(self, owner_id: UUID, run_id: UUID) -> QueryRun | None:
        """Owned run with its citations."""
        ...

    def citation(
        self, owner_id: UUID, run_id: UUID, citation_id: UUID
    ) -> StoredCitation | None:
        """One citation of an owned run."""
        ...

    def request_cancel(self, owner_id: UUID, run_id: UUID) -> QueryRun | None:
        """Queued: cancel now. Running: flag for cooperative cancel."""
        ...

    # --- worker side -------------------------------------------------------

    def start(self, run_id: UUID) -> RunWork | None:
        """Mark running and load its work; ``None`` if cancelled or terminal."""
        ...

    def reconcile_runs(self) -> int:
        """Expire exhausted run jobs and settle runs whose job is terminal.

        One transaction; idempotent; safe after any crash or restart.
        """
        ...

    def cancel_requested(self, run_id: UUID) -> bool:
        """Whether a cancel was requested (polled while running)."""
        ...

    def complete(self, fence: JobFence, run_id: UUID, done: RunCompletion) -> str:
        """``completed``/``cancelled``/``lost`` written atomically with the job."""
        ...

    def fail(
        self, fence: JobFence | None, run_id: UUID, code: str, reason: str | None
    ) -> bool:
        """Terminal failure of a non-terminal run (fenced job when given)."""
        ...

    def finish_cancelled(self, fence: JobFence, run_id: UUID) -> bool:
        """Terminal cancellation of a running run and its job."""
        ...
