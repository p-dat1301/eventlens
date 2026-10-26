from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.event_pipeline.application.use_cases.pipeline_runner import (
    PipelineRunner,
    RunnerPorts,
)
from backend.event_pipeline.application.use_cases.process_bundle import (
    BundleValidation,
    process_bundle_json,
    validate_bundle_json,
)
from backend.event_pipeline.domain.models import (
    GateReason,
    GateVerdict,
    WriteDisposition,
)
from backend.event_pipeline.infra.fake_extractor import FakeEventExtractor
from backend.event_pipeline.infra.memory_writer import InMemoryEventWriter

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"


def make_runner() -> PipelineRunner:
    return PipelineRunner(
        ports=RunnerPorts(extractor=FakeEventExtractor(), writer=InMemoryEventWriter()),
        max_items=1,
    )


def test_validation_use_case_summarizes_valid_bundle() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")

    # When
    result = validate_bundle_json(fixture_json)

    # Then
    assert result == BundleValidation(
        article_id="550e8400-e29b-41d4-a716-446655440000",
        media_count=2,
    )


def test_process_bundle_json_runs_valid_bundle() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")

    # When
    trace = process_bundle_json(fixture_json, make_runner())

    # Then
    assert str(trace.article_id) == "550e8400-e29b-41d4-a716-446655440000"
    assert trace.verdict is GateVerdict.ACCEPT
    assert trace.reasons == (GateReason.READY,)
    assert trace.write_disposition is WriteDisposition.INSERTED


def test_process_bundle_json_rejects_invalid_json() -> None:
    # Given
    invalid_json = "{}"

    # When / Then
    with pytest.raises(ValidationError):
        _ = process_bundle_json(invalid_json, make_runner())


def test_process_bundle_json_reports_unchanged_for_replay() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")
    runner = make_runner()
    _ = process_bundle_json(fixture_json, runner)

    # When
    trace = process_bundle_json(fixture_json, runner)

    # Then
    assert trace.write_disposition is WriteDisposition.UNCHANGED
