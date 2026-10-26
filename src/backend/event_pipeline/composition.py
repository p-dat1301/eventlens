"""Event pipeline composition root: wires the local deterministic adapters."""

from backend.event_pipeline.application.use_cases.pipeline_runner import (
    PipelineRunner,
    RunnerPorts,
)
from backend.event_pipeline.infra.fake_extractor import FakeEventExtractor
from backend.event_pipeline.infra.memory_writer import InMemoryEventWriter


def local_pipeline() -> PipelineRunner:
    """One-item runner over the fake extractor and a fresh in-memory writer."""
    return PipelineRunner(
        ports=RunnerPorts(
            extractor=FakeEventExtractor(),
            writer=InMemoryEventWriter(),
        ),
        max_items=1,
    )
