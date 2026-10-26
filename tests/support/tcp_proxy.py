"""Loopback TCP forwarder used to cut and restore a real PostgreSQL connection.

``stop`` closes the listener and every relayed socket (the database keeps
running and keeps its data); ``start`` listens again on the same port. Only
127.0.0.1 is ever bound or dialled.
"""

import contextlib
import socket
import threading
from dataclasses import dataclass, field


@dataclass(slots=True)
class TcpProxy:
    target_port: int
    port: int = 0
    _listener: socket.socket | None = None
    _sockets: list[socket.socket] = field(default_factory=list[socket.socket])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen()
        self.port = listener.getsockname()[1]
        self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            sockets, self._sockets = self._sockets, []
        for item in [*sockets, *([listener] if listener else [])]:
            with contextlib.suppress(OSError):
                item.shutdown(socket.SHUT_RDWR)
            item.close()

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client = listener.accept()[0]
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self.target_port))
            with self._lock:
                self._sockets.extend((client, upstream))
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pipe, args=(source, sink), daemon=True
                ).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            for item in (source, sink):
                with contextlib.suppress(OSError):
                    item.shutdown(socket.SHUT_RDWR)
                item.close()
