"""Text-free stage output of the product pipeline (selection B, payload C)."""

from dataclasses import dataclass

from backend.rag.evaluation.metrics import Json


@dataclass(frozen=True, slots=True)
class StageSpan:
    """One selected/sent span (offsets on the RAG NFC text)."""

    article_id: str
    content_hash: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class EvidenceStages:
    """Text-free B/C stage output and per-query counters."""

    selected: tuple[StageSpan, ...]
    payload: tuple[StageSpan, ...]
    payload_tokens: int
    token_budget: int
    truncated_spans: int
    omitted_articles: int
    overlap_dropped: int
    cap_dropped: int
    branch_cap_dropped: int
    backfill_calls: int
    dropped_at_final_check: int
    storage_calls: int
    embedding_calls: int
    freshness: str
    freshness_boosted: int
    empty_reason: str | None
    fill_spans_sent: int = 0
    freshness_max_promotion: int = 0
    # Per-layer trace (IDs and offsets only): branch candidates, fused list,
    # every selection group, the final-check pool, payload and drop counters.
    trace: dict[str, Json] | None = None
