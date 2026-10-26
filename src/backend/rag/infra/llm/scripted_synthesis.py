"""Scripted chat synthesizer for error and race tests (no network).

Each call consumes the next script item: a response is returned, a
``SynthesisError`` is raised, a callable is invoked with the request (it may
block on an Event to model an in-flight call). Every request is recorded.
An exhausted script raises ``provider_unavailable`` so extra calls are loud.
"""

from collections.abc import Callable, Sequence
from threading import Lock

from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.domain.synthesis import ChatModelIdentity
from backend.rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisError,
    SynthesisProviderError,
)

type ScriptItem = (
    SynthesisResponse | SynthesisError | Callable[[SynthesisRequest], SynthesisResponse]
)


class ScriptedSynthesizer:
    """``SynthesisModelPort`` that replays a fixed script."""

    def __init__(
        self, identity: ChatModelIdentity, script: Sequence[ScriptItem]
    ) -> None:
        """Copy the script; record requests in ``requests``."""
        self._identity: ChatModelIdentity = identity
        self._script: list[ScriptItem] = list(script)
        self._lock: Lock = Lock()
        self.requests: list[SynthesisRequest] = []

    @property
    def identity(self) -> ChatModelIdentity:
        """Configured chat model identity."""
        return self._identity

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Replay the next item."""
        with self._lock:
            self.requests.append(request)
            item = self._script.pop(0) if self._script else None
        if item is None:
            raise SynthesisProviderError(ProviderFailureCode.UNAVAILABLE)
        if isinstance(item, SynthesisError):
            raise item
        if isinstance(item, SynthesisResponse):
            return item
        return item(request)
