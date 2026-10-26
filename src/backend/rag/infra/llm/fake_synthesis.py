"""Deterministic fake chat synthesizer (default; no network, no cost).

It reads only the allowlisted user payload it was sent and drafts one claim
per source article citing that article's first sent span. Claim text is the
span text itself (bounded), so output is reproducible. It proves wiring, not
answer quality.
"""

import json
from typing import cast

from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.domain.synthesis import ChatModelIdentity, UsageStatus
from backend.rag.domain.synthesis_draft import (
    DRAFT_SCHEMA_VERSION,
    MAX_CLAIM_CHARS,
    MAX_DRAFT_CLAIMS,
)
from backend.rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisProviderError,
)

_CLAIM_CHARS = min(MAX_CLAIM_CHARS, 300)


def _sources(request: SynthesisRequest) -> list[dict[str, object]]:
    users = [m for m in request.serialized.messages if m.role == "user"]
    if not users:
        raise SynthesisProviderError(ProviderFailureCode.BAD_SHAPE)
    try:
        body = cast("dict[str, object]", json.loads(users[0].content))
    except ValueError:
        raise SynthesisProviderError(ProviderFailureCode.BAD_SHAPE) from None
    return cast("list[dict[str, object]]", body.get("sources", []))


class DeterministicFakeSynthesizer:
    """``SynthesisModelPort`` fake: cites the first sent span per article."""

    def __init__(self, identity: ChatModelIdentity) -> None:
        """Fix the reported model identity."""
        self._identity: ChatModelIdentity = identity

    @property
    def identity(self) -> ChatModelIdentity:
        """Configured chat model identity."""
        return self._identity

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Return a strict draft built only from the sent payload."""
        claims: list[dict[str, object]] = []
        for source in _sources(request)[:MAX_DRAFT_CLAIMS]:
            spans = cast("list[dict[str, str]]", source.get("spans", []))
            if not spans:
                continue
            text = " ".join(spans[0]["text"].split())[:_CLAIM_CHARS].strip()
            if not text:
                continue
            claims.append(
                {
                    "claim_ref": f"c{len(claims) + 1}",
                    "text": text,
                    "evidence_span_ids": [spans[0]["span_id"]],
                    "temporal_claim": None,
                }
            )
        draft: dict[str, object] = {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "evidence_status": "sufficient" if claims else "insufficient",
            "claims": claims,
            "uncertainties": (
                []
                if claims
                else [{"reason_code": "insufficient_evidence", "claim_ref": None}]
            ),
        }
        return SynthesisResponse(
            content=json.dumps(draft, ensure_ascii=False),
            usage=None,
            usage_status=UsageStatus.MISSING,
            reported_model=self._identity.model_id,
        )
