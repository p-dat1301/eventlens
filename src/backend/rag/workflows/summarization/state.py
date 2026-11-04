"""Per-invocation channels of the summarization workflow."""

from typing import TypedDict

from backend.rag.application.ports.summary import SummaryModelResponse
from backend.rag.domain.summary import (
    SummaryFailureCode,
    SummaryOutcome,
    SummaryResult,
    SummarySource,
)
from backend.rag.domain.work_jobs import ClaimedWorkJob


class SummaryChannels(TypedDict, total=False):
    """Per-invocation channels; nothing persists between invocations."""

    job: ClaimedWorkJob
    source: SummarySource
    response: SummaryModelResponse
    result: SummaryResult
    failure: SummaryFailureCode
    outcome: SummaryOutcome
