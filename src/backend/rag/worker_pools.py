"""Supervised polling loops for ``eventlens-rag-worker serve``.

A ``PoolWorker`` repeats one step (claim + process one job). Any exception
from a step (typed storage errors such as ``ConversationStorageError``, or
anything unexpected) is logged by type/code only, backed off exponentially
and retried, so a transient outage never kills the thread. ``supervise``
reports a thread that died anyway (``BaseException``) so the process can
exit non-zero and be restarted by its process manager; leases make the
abandoned job re-claimable.
"""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

MAX_BACKOFF_SECONDS: Final = 30.0

type Log = Callable[[dict[str, object]], None]


@dataclass(slots=True)
class PoolWorker:
    """One supervised loop; ``step`` returns whether it processed work."""

    name: str
    step: Callable[[], bool]
    stop: threading.Event
    log: Log
    poll_seconds: float = 1.0
    on_work: Callable[[], None] = lambda: None
    failures: int = 0
    errors: int = 0
    thread: threading.Thread | None = field(default=None, repr=False)

    def run(self) -> None:
        """Loop until ``stop``; errors back off and never end the loop."""
        while not self.stop.is_set():
            try:
                worked = self.step()
            except Exception as error:  # noqa: BLE001 - supervised boundary
                self.failures += 1
                self.errors += 1
                delay: float = min(
                    self.poll_seconds * 2.0 ** min(self.failures, 10),
                    MAX_BACKOFF_SECONDS,
                )
                self.log(
                    {
                        "event": "worker_error",
                        "worker": self.name,
                        "error": type(error).__name__,
                        "code": cast("object", getattr(error, "code", None)),
                        "retry_in_seconds": delay,
                    }
                )
                _ = self.stop.wait(delay)
                continue
            self.failures = 0
            if worked:
                self.on_work()
            else:
                _ = self.stop.wait(self.poll_seconds)

    def start(self) -> None:
        """Run in a daemon thread."""
        self.thread = threading.Thread(target=self.run, name=self.name, daemon=True)
        self.thread.start()


def dead_workers(workers: Sequence[PoolWorker]) -> tuple[str, ...]:
    """Names of started workers whose thread is no longer alive."""
    return tuple(
        w.name for w in workers if w.thread is not None and not w.thread.is_alive()
    )
