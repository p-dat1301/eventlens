"""Opaque source-reference registry port for the source viewer (D37).

Adapters raise ``RetrievalStorageError`` when their storage is unavailable and
never turn such failures into "not found".
"""

from dataclasses import dataclass
from typing import Protocol

from eventlens_contracts.evidence import EvidenceSpan
from eventlens_rag.domain.access import CallerScope


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Server-side meaning of one opaque reference."""

    principal_id: str
    scope_id: str
    span: EvidenceSpan


class SourceReferenceRegistry(Protocol):
    """Issue and resolve opaque, caller-bound references."""

    def issue(self, caller: CallerScope, span: EvidenceSpan) -> str:
        """Return a new random token for ``span`` bound to ``caller``."""
        ...

    def resolve(self, token: str) -> SourceReference | None:
        """Look a token up; ``None`` when unknown, expired or evicted."""
        ...
