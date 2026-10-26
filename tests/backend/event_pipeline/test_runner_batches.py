from pathlib import Path
from typing import final
from uuid import UUID

import pytest

from backend.event_pipeline.application.ports import EventWriteConflictError
from backend.event_pipeline.application.use_cases.pipeline_runner import (
    BatchAbortedError,
    PipelineRunner,
    RunnerPorts,
)
from backend.event_pipeline.domain.models import (
    EventProposal,
    GateReason,
    GateVerdict,
    ProcessingTrace,
    WriteDisposition,
    WriteResult,
)
from backend.event_pipeline.infra.fake_extractor import FakeEventExtractor
from backend.event_pipeline.infra.memory_writer import InMemoryEventWriter
from backend.shared.contracts.bundle import ArticleBundle

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"
FIRST_ARTICLE_ID = UUID("869dbd2d-6188-4d5c-b68e-f1b686aee718")
THIRD_ARTICLE_ID = UUID("a3151c66-96f0-4cb1-8146-46c857a22184")


@final
class ConflictingEventWriter:
    """Raise port-defined conflict on configured write call."""

    def __init__(self, conflict_on_call: int) -> None:
        self._conflict_on_call = conflict_on_call
        self._calls = 0

    def write(self, proposal: EventProposal) -> WriteResult:
        self._calls += 1
        if self._calls == self._conflict_on_call:
            raise EventWriteConflictError(event_id=proposal.event.id)
        return WriteResult(
            disposition=WriteDisposition.INSERTED,
        )


def load_bundle() -> ArticleBundle:
    return ArticleBundle.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


def with_article_id(bundle: ArticleBundle, article_id: UUID) -> ArticleBundle:
    article = bundle.article.model_copy(update={"article_id": article_id})
    media = tuple(
        item.model_copy(update={"article_id": article_id}) for item in bundle.media
    )
    return bundle.model_copy(update={"article": article, "media": media})


def test_run_exposes_completed_trace_when_later_write_conflicts() -> None:
    # Given
    second_bundle = load_bundle()
    first_bundle = with_article_id(second_bundle, FIRST_ARTICLE_ID)
    extractor = FakeEventExtractor()
    writer = ConflictingEventWriter(conflict_on_call=2)
    runner = PipelineRunner(
        ports=RunnerPorts(extractor=extractor, writer=writer),
        max_items=2,
    )

    # When
    with pytest.raises(BatchAbortedError) as captured:
        _ = runner.run((first_bundle, second_bundle))

    # Then
    first_proposal = extractor.extract(first_bundle)
    second_proposal = extractor.extract(second_bundle)
    assert captured.value.completed == (
        ProcessingTrace(
            schema_version="processing_trace.v1",
            article_id=first_bundle.article.article_id,
            proposal_id=first_proposal.proposal_id,
            event_id=first_proposal.event.id,
            verdict=GateVerdict.ACCEPT,
            reasons=(GateReason.READY,),
            write_disposition=WriteDisposition.INSERTED,
        ),
    )
    assert captured.value.event_id == second_proposal.event.id


def test_run_wraps_first_writer_conflict_with_no_completed_items() -> None:
    # Given
    bundle = load_bundle()
    extractor = FakeEventExtractor()
    runner = PipelineRunner(
        ports=RunnerPorts(
            extractor=extractor,
            writer=ConflictingEventWriter(conflict_on_call=1),
        ),
        max_items=1,
    )

    # When
    with pytest.raises(BatchAbortedError) as captured:
        _ = runner.run((bundle,))

    # Then
    assert captured.value.completed == ()
    assert captured.value.event_id == extractor.extract(bundle).event.id


def test_run_returns_mixed_verdicts_in_bundle_order() -> None:
    # Given
    accepted = load_bundle()
    rejected_base = with_article_id(accepted, FIRST_ARTICLE_ID)
    duplicate = rejected_base.article.duplicate.model_copy(
        update={"is_duplicate": True}
    )
    rejected = rejected_base.model_copy(
        update={
            "article": rejected_base.article.model_copy(update={"duplicate": duplicate})
        }
    )
    reviewed_base = with_article_id(accepted, THIRD_ARTICLE_ID)
    processing = reviewed_base.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    reviewed = reviewed_base.model_copy(
        update={
            "article": reviewed_base.article.model_copy(
                update={"processing": processing}
            )
        }
    )
    runner = PipelineRunner(
        ports=RunnerPorts(
            extractor=FakeEventExtractor(),
            writer=InMemoryEventWriter(),
        ),
        max_items=3,
    )

    # When
    traces = runner.run((accepted, rejected, reviewed))

    # Then
    assert len(traces) == 3
    assert tuple(trace.verdict for trace in traces) == (
        GateVerdict.ACCEPT,
        GateVerdict.REJECT,
        GateVerdict.REVIEW,
    )
    assert tuple(trace.article_id for trace in traces) == (
        accepted.article.article_id,
        rejected.article.article_id,
        reviewed.article.article_id,
    )
