from uuid import UUID

import pytest
from pydantic import ValidationError

from eventlens_worker.models import (
    GateReason,
    GateVerdict,
    ProcessingTrace,
    WriteDisposition,
)

ARTICLE_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
PROPOSAL_ID = UUID("d67c8ef5-5685-4182-a8d7-fdb01191433e")
EVENT_ID = UUID("811c7e67-1874-4d90-aabe-ce7ea45f4e4c")
VALID_TRACE_JSON = f"""{{
    "article_id": "{ARTICLE_ID}",
    "proposal_id": "{PROPOSAL_ID}",
    "event_id": "{EVENT_ID}",
    "verdict": "ACCEPT",
    "reasons": ["ready"],
    "write_disposition": "inserted"
}}"""


def make_trace(
    verdict: GateVerdict,
    reasons: tuple[GateReason, ...],
    disposition: WriteDisposition | None,
) -> ProcessingTrace:
    return ProcessingTrace(
        schema_version="processing_trace.v1",
        article_id=ARTICLE_ID,
        proposal_id=PROPOSAL_ID,
        event_id=EVENT_ID,
        verdict=verdict,
        reasons=reasons,
        write_disposition=disposition,
    )


def test_trace_rejects_missing_schema_version() -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = ProcessingTrace.model_validate_json(VALID_TRACE_JSON)

    # Then
    assert captured.value.errors()[0]["type"] == "missing"


def test_trace_rejects_wrong_schema_version() -> None:
    # Given
    wrong_version_json = VALID_TRACE_JSON.replace(
        '"article_id"', '"schema_version": "processing_trace.v2",\n    "article_id"'
    )

    # When
    with pytest.raises(ValidationError) as captured:
        _ = ProcessingTrace.model_validate_json(wrong_version_json)

    # Then
    assert captured.value.errors()[0]["type"] == "literal_error"


def test_trace_wire_output_includes_exact_schema_version() -> None:
    # Given
    trace = make_trace(
        GateVerdict.ACCEPT,
        (GateReason.READY,),
        WriteDisposition.INSERTED,
    )

    # When
    wire_output = trace.model_dump(mode="json")

    # Then
    assert wire_output["schema_version"] == "processing_trace.v1"


@pytest.mark.parametrize("disposition", tuple(WriteDisposition))
def test_accept_trace_allows_ready_with_completed_write(
    disposition: WriteDisposition,
) -> None:
    # Given / When
    trace = make_trace(GateVerdict.ACCEPT, (GateReason.READY,), disposition)

    # Then
    assert trace.reasons == (GateReason.READY,)
    assert trace.write_disposition is disposition
    assert trace.model_config.get("frozen") is True


@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param((), id="empty"),
        pytest.param((GateReason.LOW_CONFIDENCE,), id="non-ready"),
        pytest.param(
            (GateReason.READY, GateReason.FLAGGED_INPUT),
            id="ready-with-other",
        ),
    ],
)
def test_accept_trace_rejects_reasons_other_than_ready_only(
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_trace(GateVerdict.ACCEPT, reasons, WriteDisposition.INSERTED)

    # Then
    assert captured.value.errors()[0]["type"] == "processing_trace_accept_reasons"


def test_accept_trace_rejects_missing_write_disposition() -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_trace(GateVerdict.ACCEPT, (GateReason.READY,), None)

    # Then
    assert captured.value.errors()[0]["type"] == "processing_trace_accept_write"


def test_reject_trace_allows_low_confidence_without_write() -> None:
    # Given / When
    trace = make_trace(GateVerdict.REJECT, (GateReason.LOW_CONFIDENCE,), None)

    # Then
    assert trace.reasons == (GateReason.LOW_CONFIDENCE,)
    assert trace.write_disposition is None


@pytest.mark.parametrize("verdict", [GateVerdict.REJECT, GateVerdict.REVIEW])
def test_non_accept_trace_rejects_empty_reasons(verdict: GateVerdict) -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_trace(verdict, (), None)

    # Then
    assert captured.value.errors()[0]["type"] == "processing_trace_reasons_required"


@pytest.mark.parametrize("verdict", [GateVerdict.REJECT, GateVerdict.REVIEW])
@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param((GateReason.READY,), id="ready-only"),
        pytest.param(
            (GateReason.READY, GateReason.LOW_CONFIDENCE),
            id="ready-with-other",
        ),
    ],
)
def test_non_accept_trace_rejects_ready_reason(
    verdict: GateVerdict,
    reasons: tuple[GateReason, ...],
) -> None:
    # Given / When
    with pytest.raises(ValidationError) as captured:
        _ = make_trace(verdict, reasons, None)

    # Then
    assert captured.value.errors()[0]["type"] == "processing_trace_ready_prohibited"


@pytest.mark.parametrize("verdict", [GateVerdict.REJECT, GateVerdict.REVIEW])
@pytest.mark.parametrize("disposition", tuple(WriteDisposition))
def test_non_accept_trace_rejects_write_disposition(
    verdict: GateVerdict,
    disposition: WriteDisposition,
) -> None:
    # Given
    reasons = {
        GateVerdict.REJECT: (GateReason.LOW_CONFIDENCE,),
        GateVerdict.REVIEW: (GateReason.FLAGGED_INPUT,),
    }[verdict]

    # When
    with pytest.raises(ValidationError) as captured:
        _ = make_trace(verdict, reasons, disposition)

    # Then
    assert captured.value.errors()[0]["type"] == "processing_trace_write_prohibited"
