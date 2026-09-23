"""Deterministic policy gate for extracted event proposals."""

from typing import Final

from eventlens_contracts import ArticleBundle
from eventlens_worker.models import (
    EventProposal,
    GateDecision,
    GateReason,
    GateVerdict,
)

MINIMUM_CONFIDENCE: Final = 0.50


def decide_event(bundle: ArticleBundle, proposal: EventProposal) -> GateDecision:
    """Return ordered gate reasons and highest-priority applicable verdict."""
    article_id_mismatch = proposal.article_id != bundle.article.article_id
    duplicate_article = bundle.article.duplicate.is_duplicate
    event_confidence = proposal.event.confidence
    low_confidence = event_confidence is None or event_confidence < MINIMUM_CONFIDENCE
    missing_event_time = proposal.event.time_start is None

    flagged_input = bundle.article.processing.quality_status == "flagged"

    reasons = (
        *((GateReason.ARTICLE_ID_MISMATCH,) if article_id_mismatch else ()),
        *((GateReason.DUPLICATE_ARTICLE,) if duplicate_article else ()),
        *((GateReason.LOW_CONFIDENCE,) if low_confidence else ()),
        *((GateReason.FLAGGED_INPUT,) if flagged_input else ()),
        *((GateReason.MISSING_EVENT_TIME,) if missing_event_time else ()),
    )
    if article_id_mismatch or duplicate_article or low_confidence:
        return GateDecision(verdict=GateVerdict.REJECT, reasons=reasons)
    if reasons:
        return GateDecision(verdict=GateVerdict.REVIEW, reasons=reasons)
    return GateDecision(
        verdict=GateVerdict.ACCEPT,
        reasons=(GateReason.READY,),
    )
