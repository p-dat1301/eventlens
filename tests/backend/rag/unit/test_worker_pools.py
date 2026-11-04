"""Supervised worker loops survive step errors and report dead threads."""

import threading

from backend.rag.domain.conversation import ConversationStorageError
from backend.rag.worker_pools import PoolWorker, dead_workers


def test_transient_storage_error_is_logged_backed_off_and_retried() -> None:
    stop = threading.Event()
    events: list[dict[str, object]] = []
    calls: list[int] = []

    def step() -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise ConversationStorageError
        if len(calls) >= 3:
            stop.set()
        return True

    worker = PoolWorker("w", step, stop, events.append, poll_seconds=0.01)
    worker.start()
    assert worker.thread is not None
    worker.thread.join(timeout=5)

    assert len(calls) == 3  # kept working after the error
    assert worker.errors == 1
    assert events[0]["error"] == "ConversationStorageError"
    assert events[0]["code"] == "conversation_storage_unavailable"
    assert dead_workers([worker]) == ("w",)  # stopped normally; thread ended


def test_unexpected_errors_never_end_the_loop() -> None:
    stop = threading.Event()
    seen: list[int] = []

    def step() -> bool:
        seen.append(1)
        if len(seen) > 4:
            stop.set()
            return False
        raise AttributeError

    worker = PoolWorker("w", step, stop, lambda _: None, poll_seconds=0.001)
    worker.run()
    assert worker.errors == 4


def test_dead_thread_is_reported_for_process_restart() -> None:
    worker = PoolWorker("doomed", lambda: False, threading.Event(), lambda _: None)
    alive = PoolWorker("alive", lambda: False, threading.Event(), lambda _: None)
    worker.thread = threading.Thread(target=lambda: None)  # ended unexpectedly
    worker.thread.start()
    worker.thread.join()
    assert dead_workers([worker, alive]) == ("doomed",)  # unstarted are ignored
