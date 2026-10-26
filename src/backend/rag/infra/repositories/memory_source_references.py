"""Bounded process-local source references (fixture runtime and tests)."""

import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

from backend.rag.application.ports.source_references import SourceReference
from backend.rag.domain.access import CallerScope
from backend.shared.contracts.evidence import EvidenceSpan


@dataclass(slots=True)
class InMemorySourceReferences:  # noqa: MUTABLE_OK - bounded process-local map
    """LRU token map; one per process, lost on restart (fails closed)."""

    capacity: int = 4096
    _entries: OrderedDict[str, SourceReference] = field(
        default_factory=OrderedDict[str, SourceReference]
    )
    _lock: Lock = field(default_factory=Lock)

    def issue(self, caller: CallerScope, span: EvidenceSpan) -> str:
        """Store a reference under a fresh random token."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._entries[token] = SourceReference(
                caller.principal_id, caller.scope_id, span
            )
            while len(self._entries) > self.capacity:
                _ = self._entries.popitem(last=False)
        return token

    def resolve(self, token: str) -> SourceReference | None:
        """Return the reference without widening its lifetime."""
        with self._lock:
            return self._entries.get(token)
