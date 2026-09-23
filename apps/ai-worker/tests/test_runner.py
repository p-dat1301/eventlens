from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import final
from uuid import UUID

import pytest

from eventlens_contracts import ArticleBundle
from eventlens_worker.fake_extractor import FakeEventExtractor
from eventlens_worker.memory_writer import InMemoryEventWriter
from eventlens_worker.models import (
    EventProposal,
    GateReason,
    GateVerdict,
    WriteDisposition,
    WriteResult,
)
from eventlens_worker.runner import (
    BatchAbortedError,
    BatchLimitExceededError,
    InvalidRunnerLimitError,
    PipelineRunner,
    RunnerPorts,
)

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"
SECOND_ARTICLE_ID = UUID("869dbd2d-6188-4d5c-b68e-f1b686aee718")


def load_bundle() -> ArticleBundle:
    return ArticleBundle.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


@final
class RecordingExtractor:
    """Record calls while delegating extraction to deterministic adapter."""

    def __init__(self) -> None:
        self.article_ids: list[UUID] = []
        self._delegate = FakeEventExtractor()

    def extract(self, bundle: ArticleBundle) -> EventProposal:
        self.article_ids.append(bundle.article.article_id)
        return self._delegate.extract(bundle)


@final
class RecordingWriter:
    """Record calls while delegating writes to in-memory adapter."""

    def __init__(self) -> None:
        self.proposal_ids: list[UUID] = []
        self._delegate = InMemoryEventWriter()

    def write(self, proposal: EventProposal) -> WriteResult:
        self.proposal_ids.append(proposal.proposal_id)
        return self._delegate.write(proposal)


def make_runner(
    extractor: RecordingExtractor,
    writer: RecordingWriter,
    *,
    max_items: int = 10,
) -> PipelineRunner:
    return PipelineRunner(
        ports=RunnerPorts(extractor=extractor, writer=writer),
        max_items=max_items,
    )


def test_run_accepts_and_inserts_clean_bundle_with_exact_trace() -> None:
    # Given
    bundle = load_bundle()
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    traces = make_runner(extractor, writer).run((bundle,))

    # Then
    proposal = FakeEventExtractor().extract(bundle)
    assert len(traces) == 1
    assert traces[0].article_id == bundle.article.article_id
    assert traces[0].proposal_id == proposal.proposal_id
    assert traces[0].event_id == proposal.event.id
    assert traces[0].verdict is GateVerdict.ACCEPT
    assert traces[0].reasons == (GateReason.READY,)
    assert traces[0].write_disposition is WriteDisposition.INSERTED


def test_run_uses_proposal_event_id_for_accepted_trace() -> None:
    # Given
    bundle = load_bundle()
    proposal = FakeEventExtractor().extract(bundle)
    runner = PipelineRunner(
        ports=RunnerPorts(
            extractor=FakeEventExtractor(),
            writer=RecordingWriter(),
        ),
        max_items=1,
    )

    # When
    trace = runner.run((bundle,))[0]

    # Then
    assert trace.event_id == proposal.event.id


def test_run_reports_unchanged_for_exact_replay() -> None:
    # Given
    bundle = load_bundle()
    runner = make_runner(RecordingExtractor(), RecordingWriter())
    _ = runner.run((bundle,))

    # When
    traces = runner.run((bundle,))

    # Then
    assert traces[0].write_disposition is WriteDisposition.UNCHANGED


def test_run_rejects_duplicate_without_writing() -> None:
    # Given
    bundle = load_bundle()
    duplicate = bundle.article.duplicate.model_copy(update={"is_duplicate": True})
    rejected = bundle.model_copy(
        update={"article": bundle.article.model_copy(update={"duplicate": duplicate})}
    )
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    traces = make_runner(extractor, writer).run((rejected,))

    # Then
    assert traces[0].verdict is GateVerdict.REJECT
    assert traces[0].reasons == (GateReason.DUPLICATE_ARTICLE,)
    assert traces[0].write_disposition is None
    assert writer.proposal_ids == []


def test_run_routes_flagged_bundle_to_review_without_writing() -> None:
    # Given
    bundle = load_bundle()
    processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    reviewed = bundle.model_copy(
        update={"article": bundle.article.model_copy(update={"processing": processing})}
    )
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    traces = make_runner(extractor, writer).run((reviewed,))

    # Then
    assert traces[0].verdict is GateVerdict.REVIEW
    assert traces[0].reasons == (GateReason.FLAGGED_INPUT,)
    assert traces[0].write_disposition is None
    assert writer.proposal_ids == []


def test_run_preserves_bundle_order() -> None:
    # Given
    first = load_bundle()
    second_article = first.article.model_copy(update={"article_id": SECOND_ARTICLE_ID})
    second_media = tuple(
        item.model_copy(update={"article_id": SECOND_ARTICLE_ID})
        for item in first.media
    )
    second = first.model_copy(update={"article": second_article, "media": second_media})
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    traces = make_runner(extractor, writer).run((first, second))

    # Then
    expected_ids = [first.article.article_id, SECOND_ARTICLE_ID]
    assert [trace.article_id for trace in traces] == expected_ids
    assert extractor.article_ids == expected_ids


def test_run_accepts_batch_at_limit() -> None:
    # Given
    bundle = load_bundle()
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    traces = make_runner(extractor, writer, max_items=1).run((bundle,))

    # Then
    assert len(traces) == 1
    assert len(extractor.article_ids) == 1
    assert len(writer.proposal_ids) == 1


def test_run_rejects_batch_above_limit_before_adapter_calls() -> None:
    # Given
    bundle = load_bundle()
    extractor = RecordingExtractor()
    writer = RecordingWriter()

    # When
    with pytest.raises(BatchLimitExceededError) as captured:
        _ = make_runner(extractor, writer, max_items=1).run((bundle, bundle))

    # Then
    assert captured.value == BatchLimitExceededError(limit=1, received=2)
    assert str(captured.value) == "batch limit 1 exceeded by 2 received items"
    assert extractor.article_ids == []
    assert writer.proposal_ids == []


@pytest.mark.parametrize("limit", [0, -1])
def test_constructor_rejects_invalid_limit(limit: int) -> None:
    # Given
    ports = RunnerPorts(extractor=RecordingExtractor(), writer=RecordingWriter())

    # When
    with pytest.raises(InvalidRunnerLimitError) as captured:
        _ = PipelineRunner(ports=ports, max_items=limit)

    # Then
    assert captured.value == InvalidRunnerLimitError(limit=limit)
    assert str(captured.value) == f"runner limit must be at least 1, got {limit}"


def test_runner_ports_are_frozen() -> None:
    # Given
    ports = RunnerPorts(extractor=RecordingExtractor(), writer=RecordingWriter())

    # When / Then
    extractor_field = "extractor"
    with pytest.raises(FrozenInstanceError):
        setattr(ports, extractor_field, RecordingExtractor())


def test_run_wraps_writer_conflict_on_first_item() -> None:
    # Given
    bundle = load_bundle()
    runner = PipelineRunner(
        ports=RunnerPorts(extractor=FakeEventExtractor(), writer=InMemoryEventWriter()),
        max_items=1,
    )
    _ = runner.run((bundle,))
    changed = bundle.model_copy(
        update={"article": bundle.article.model_copy(update={"title": "Changed title"})}
    )

    # When / Then
    with pytest.raises(BatchAbortedError) as captured:
        _ = runner.run((changed,))

    assert captured.value.completed == ()
    assert captured.value.event_id == FakeEventExtractor().extract(changed).event.id
