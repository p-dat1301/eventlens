"""Process-local shared admission limiter (D32 concurrency, spec section 8).

One ``ProcessConcurrencyLimiter`` instance is created per runtime/composition
and shared by every request; it never queues. A lease's slot is freed only
once the owner released it *and* every pin is undone, so a provider call that
outlives a cancelled or timed-out request keeps occupying capacity until it
returns. This is a single-process guarantee, not a distributed limit.
"""

from collections.abc import Callable
from threading import Lock

from backend.rag.domain.synthesis_errors import (
    SynthesisConcurrencyRejectedError,
    SynthesisConfigurationError,
)
from backend.rag.domain.synthesis_policy import HARD_MAX_CONCURRENCY


class _Lease:
    """One admitted slot; implements the ``AdmissionLease`` port."""

    def __init__(self, limiter: "ProcessConcurrencyLimiter") -> None:
        self._limiter: ProcessConcurrencyLimiter = limiter
        self._released: bool = False
        self._pins: int = 0
        self._freed: bool = False

    @property
    def released(self) -> bool:
        """Whether the owner released the lease."""
        with self._limiter.lock:
            return self._released

    def _maybe_free(self) -> None:
        if self._released and self._pins == 0 and not self._freed:
            self._freed = True
            self._limiter.free_slot()

    def pin(self) -> Callable[[], None]:
        """Hold the slot for in-flight work; the returned unpin is idempotent."""
        with self._limiter.lock:
            if self._freed or self._released:
                message = "cannot pin a released admission lease"
                raise SynthesisConfigurationError(message)
            self._pins += 1
        done = False

        def unpin() -> None:
            nonlocal done
            with self._limiter.lock:
                if done:
                    return
                done = True
                self._pins -= 1
                self._maybe_free()

        return unpin

    def release(self) -> None:
        """Owner release (idempotent); the slot stays held while pinned."""
        with self._limiter.lock:
            self._released = True
            self._maybe_free()


class ProcessConcurrencyLimiter:
    """Shared, non-queuing limiter implementing ``ConcurrencyLimiter``."""

    def __init__(self, capacity: int) -> None:
        """Composition passes ``policy.concurrency.max_concurrent_requests``."""
        if type(capacity) is not int or not 1 <= capacity <= HARD_MAX_CONCURRENCY:
            message = "concurrency capacity must be within the policy hard cap"
            raise SynthesisConfigurationError(message)
        self._capacity: int = capacity
        self._in_use: int = 0
        self.lock: Lock = Lock()

    @property
    def capacity(self) -> int:
        """Maximum concurrent admitted requests."""
        return self._capacity

    @property
    def in_use(self) -> int:
        """Slots currently held (including pinned in-flight calls)."""
        with self.lock:
            return self._in_use

    def try_acquire(self) -> _Lease:
        """Admit or raise ``SynthesisConcurrencyRejectedError``; never queue."""
        with self.lock:
            if self._in_use >= self._capacity:
                raise SynthesisConcurrencyRejectedError
            self._in_use += 1
            return _Lease(self)

    def free_slot(self) -> None:
        """Return one slot; callers must already hold ``lock``."""
        self._in_use -= 1
