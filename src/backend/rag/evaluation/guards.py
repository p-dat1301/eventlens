"""Process-local network guard: any outbound socket connect fails and is counted.

Eval runs are in-memory and fake-only; the guard turns an accidental provider
or database call into a loud, counted failure instead of silent traffic.
"""

import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NoReturn


class NetworkBlockedError(OSError):
    """Raised for every connect attempt while the guard is active."""


@dataclass(slots=True)
class NetworkAttempts:  # noqa: MUTABLE_OK - counter
    """Number of blocked connect attempts."""

    count: int = 0


@contextmanager
def no_network() -> Generator[NetworkAttempts]:
    """Block ``socket.connect``/``connect_ex``/``create_connection``."""
    attempts = NetworkAttempts()
    saved_connect = socket.socket.connect
    saved_connect_ex = socket.socket.connect_ex
    saved_create = socket.create_connection

    def refuse(*_: object, **_kwargs: object) -> NoReturn:
        attempts.count += 1
        raise NetworkBlockedError

    patched: Callable[..., NoReturn] = refuse
    socket.socket.connect = patched
    socket.socket.connect_ex = patched
    socket.create_connection = patched
    try:
        yield attempts
    finally:
        socket.socket.connect = saved_connect
        socket.socket.connect_ex = saved_connect_ex
        socket.create_connection = saved_create
