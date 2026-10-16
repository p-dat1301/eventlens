from itertools import combinations
from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens_worker.models import GateReason, GateVerdict, ProcessingTrace

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
PROPOSAL_ID = UUID("d67c8ef5-5685-4182-a8d7-fdb01191433e")
EVENT_ID = UUID("811c7e67-1874-4d90-aabe-ce7ea45f4e4c")
REJECT_REASONS = (
    GateReason.ARTICLE_ID_MISMATCH,
    GateReason.DUPLICATE_ARTICLE,
    GateReason.LOW_CONFIDENCE,
)
REVIEW_REASONS = (GateReason.FLAGGED_INPUT, GateReason.MISSING_EVENT_TIME)


def ordered_non_empty_subsets(
    reasons: tuple[GateReason, ...],
) -> tuple[tuple[GateReason, ...], ...]:
    return tuple(
        subset
        for size in range(1, len(reasons) + 1)
        for subset in combinations(reasons, size)
    )


def make_non_accept_trace(
    verdict: GateVerdict,
    reasons: tuple[GateReason, ...],
) -> ProcessingTrace:
    return ProcessingTrace(
        schema_version="processing_trace.v1",
        article_id=ARTICLE_ID,
        proposal_id=PROPOSAL_ID,
        event_id=EVENT_ID,
        verdict=verdict,
        reasons=reasons,
        write_disposition=None,
    )


REJECT_REASON_SETS = tuple(
    reject_reasons + review_reasons
    for reject_reasons in ordered_non_empty_subsets(REJECT_REASONS)
    for review_reasons in ((), *ordered_non_empty_subsets(REVIEW_REASONS))
)
REVIEW_REASON_SETS = ordered_non_empty_subsets(REVIEW_REASONS)


@pytest.mark.parametrize("reasons", REJECT_REASON_SETS)
def test_reject_trace_allows_reject_reasons_with_optional_review_reasons(
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    trace = make_non_accept_trace(GateVerdict.REJECT, reasons)

    # Then
    assert trace.reasons == reasons


@pytest.mark.parametrize("reasons", REVIEW_REASON_SETS)
def test_review_trace_allows_only_non_empty_review_reason_subsets(
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    trace = make_non_accept_trace(GateVerdict.REVIEW, reasons)

    # Then
    assert trace.reasons == reasons


@pytest.mark.parametrize("reasons", REVIEW_REASON_SETS)
def test_reject_trace_rejects_reasons_without_reject_reason(
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_non_accept_trace(GateVerdict.REJECT, reasons)

    # Then
    assert captured.value.errors()[0]["type"] == (
        "processing_trace_reject_reason_required"
    )


@pytest.mark.parametrize("reject_reason", REJECT_REASONS)
@pytest.mark.parametrize("review_reasons", [(), *REVIEW_REASON_SETS])
def test_review_trace_rejects_any_reject_reason(
    reject_reason: GateReason,
    review_reasons: tuple[GateReason, ...],
) -> None:
    # Given
    reasons = (reject_reason, *review_reasons)

    # When
    with pytest.raises(ValidationError) as captured:
        _ = make_non_accept_trace(GateVerdict.REVIEW, reasons)

    # Then
    assert captured.value.errors()[0]["type"] == (
        "processing_trace_review_reject_reason"
    )


@pytest.mark.parametrize(
    ("verdict", "reasons"),
    [
        pytest.param(
            GateVerdict.REJECT,
            (GateReason.LOW_CONFIDENCE, GateReason.LOW_CONFIDENCE),
            id="reject-duplicate",
        ),
        pytest.param(
            GateVerdict.REVIEW,
            (GateReason.FLAGGED_INPUT, GateReason.FLAGGED_INPUT),
            id="review-duplicate",
        ),
        pytest.param(
            GateVerdict.REJECT,
            (GateReason.FLAGGED_INPUT, GateReason.LOW_CONFIDENCE),
            id="reject-reversed",
        ),
        pytest.param(
            GateVerdict.REVIEW,
            (GateReason.MISSING_EVENT_TIME, GateReason.FLAGGED_INPUT),
            id="review-reversed",
        ),
    ],
)
def test_non_accept_trace_rejects_non_canonical_reason_order(
    verdict: GateVerdict,
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_non_accept_trace(verdict, reasons)

    # Then
    assert captured.value.errors()[0]["type"] == (
        "processing_trace_reasons_non_canonical"
    )
