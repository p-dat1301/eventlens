from pathlib import Path
from uuid import UUID

from backend.event_pipeline.domain.gate import decide_event
from backend.event_pipeline.domain.models import (
    EventProposal,
    GateDecision,
    GateReason,
    GateVerdict,
)
from backend.shared.contracts import Event
from backend.shared.contracts.bundle import ArticleBundle

FIXTURE_DIRECTORY = Path(__file__).parents[3] / "fixtures"
PROPOSAL_ID = UUID("d67c8ef5-5685-4182-a8d7-fdb01191433e")
MISMATCHED_ARTICLE_ID = UUID("869dbd2d-6188-4d5c-b68e-f1b686aee718")


def make_ready_inputs() -> tuple[ArticleBundle, EventProposal]:
    bundle = ArticleBundle.model_validate_json(
        (FIXTURE_DIRECTORY / "article_bundle.v1.json").read_text(encoding="utf-8")
    )
    event = Event.model_validate_json(
        (FIXTURE_DIRECTORY / "event_detected_unknown.v1.json").read_text(
            encoding="utf-8"
        )
    )
    event_with_time = Event.model_validate(
        event.model_dump() | {"confidence": 0.91, "time_start": event.first_seen_at}
    )
    proposal = EventProposal(
        proposal_id=PROPOSAL_ID,
        article_id=bundle.article.article_id,
        extractor_version="rules-2026.09",
        event=event_with_time,
    )
    return bundle, proposal


def test_decide_event_accepts_clean_proposal_at_confidence_boundary() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    boundary_proposal = proposal.model_copy(
        update={"event": proposal.event.model_copy(update={"confidence": 0.50})}
    )

    # When
    decision = decide_event(bundle, boundary_proposal)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.ACCEPT,
        reasons=(GateReason.READY,),
    )


def test_decide_event_reviews_missing_event_time_only() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    proposal_without_time = proposal.model_copy(
        update={"event": proposal.event.model_copy(update={"time_start": None})}
    )

    # When
    decision = decide_event(bundle, proposal_without_time)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REVIEW,
        reasons=(GateReason.MISSING_EVENT_TIME,),
    )


def test_decide_event_reviews_flagged_input_only() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    flagged_processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    flagged_bundle = bundle.model_copy(
        update={
            "article": bundle.article.model_copy(
                update={"processing": flagged_processing}
            )
        }
    )

    # When
    decision = decide_event(flagged_bundle, proposal)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REVIEW,
        reasons=(GateReason.FLAGGED_INPUT,),
    )


def test_decide_event_reviews_flagged_input_and_missing_time_in_canonical_order() -> (
    None
):
    # Given
    bundle, proposal = make_ready_inputs()
    flagged_processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    flagged_bundle = bundle.model_copy(
        update={
            "article": bundle.article.model_copy(
                update={"processing": flagged_processing}
            )
        }
    )
    proposal_without_time = proposal.model_copy(
        update={"event": proposal.event.model_copy(update={"time_start": None})}
    )

    # When
    decision = decide_event(flagged_bundle, proposal_without_time)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REVIEW,
        reasons=(GateReason.FLAGGED_INPUT, GateReason.MISSING_EVENT_TIME),
    )


def test_decide_event_rejects_confidence_below_boundary() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    low_confidence_proposal = proposal.model_copy(
        update={"event": proposal.event.model_copy(update={"confidence": 0.49})}
    )

    # When
    decision = decide_event(bundle, low_confidence_proposal)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REJECT,
        reasons=(GateReason.LOW_CONFIDENCE,),
    )


def test_decide_event_rejects_low_confidence_and_retains_lower_priority_reasons() -> (
    None
):
    # Given
    bundle, proposal = make_ready_inputs()
    flagged_processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    flagged_article = bundle.article.model_copy(
        update={"processing": flagged_processing}
    )
    flagged_bundle = bundle.model_copy(update={"article": flagged_article})
    review_proposal = proposal.model_copy(
        update={
            "event": proposal.event.model_copy(
                update={"confidence": 0.49, "time_start": None}
            ),
        }
    )

    # When
    decision = decide_event(flagged_bundle, review_proposal)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REJECT,
        reasons=(
            GateReason.LOW_CONFIDENCE,
            GateReason.FLAGGED_INPUT,
            GateReason.MISSING_EVENT_TIME,
        ),
    )


def test_decide_event_rejects_but_retains_every_reason_in_fixed_order() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    duplicate = bundle.article.duplicate.model_copy(update={"is_duplicate": True})
    flagged_processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged"}
    )
    rejected_article = bundle.article.model_copy(
        update={"duplicate": duplicate, "processing": flagged_processing}
    )
    rejected_bundle = bundle.model_copy(update={"article": rejected_article})
    rejected_proposal = proposal.model_copy(
        update={
            "article_id": MISMATCHED_ARTICLE_ID,
            "event": proposal.event.model_copy(
                update={"confidence": 0.10, "time_start": None}
            ),
        }
    )

    # When
    decision = decide_event(rejected_bundle, rejected_proposal)

    # Then
    assert decision == GateDecision(
        verdict=GateVerdict.REJECT,
        reasons=(
            GateReason.ARTICLE_ID_MISMATCH,
            GateReason.DUPLICATE_ARTICLE,
            GateReason.LOW_CONFIDENCE,
            GateReason.FLAGGED_INPUT,
            GateReason.MISSING_EVENT_TIME,
        ),
    )


def test_decide_event_does_not_mutate_inputs() -> None:
    # Given
    bundle, proposal = make_ready_inputs()
    original_bundle = bundle.model_copy(deep=True)
    original_proposal = proposal.model_copy(deep=True)

    # When
    _ = decide_event(bundle, proposal)

    # Then
    assert bundle == original_bundle
    assert proposal == original_proposal
