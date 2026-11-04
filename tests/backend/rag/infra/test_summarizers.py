"""OpenAI-compatible summary adapter over a recording transport (no network)."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

import pytest

from backend.rag.application.ports.summary import SummaryModelRequest
from backend.rag.application.use_cases.summarize import SummarySteps
from backend.rag.domain.summary import (
    SummaryFailureCode,
    SummaryProviderError,
    SummarySource,
)
from backend.rag.domain.work_jobs import JobKind
from backend.rag.infra.llm.openai_synthesis import (
    TransportConnectionError,
    TransportResponse,
    TransportTimeoutError,
)
from backend.rag.infra.llm.summarizers import (
    OpenAICompatibleSummarizer,
    UnconfiguredSummarizer,
)
from backend.rag.workflows.summarization.workflow import SummaryRunner
from news_support import MemoryQueue, MemorySummaries

SOURCE = SummarySource(
    uuid4(), "sha256:" + "a" * 64, "Tiêu đề", "Nội dung bài viết.", "Nguồn", None
)
REQUEST = SummaryModelRequest(SOURCE, 5.0, 300)


@dataclass(slots=True)
class Transport:
    """Scripted response or exception; records every call."""

    response: TransportResponse | Exception
    calls: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        del headers, timeout_seconds
        self.calls.append((url, dict(payload)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _ok(content: str) -> TransportResponse:
    body = {
        "model": "m",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    return TransportResponse(200, json.dumps(body).encode())


def _adapter(
    response: TransportResponse | Exception,
) -> tuple[OpenAICompatibleSummarizer, Transport]:
    transport = Transport(response)
    return (
        OpenAICompatibleSummarizer(transport, "https://models.invalid/v1", "m", "m@1"),
        transport,
    )


def test_only_the_revision_fields_are_sent_and_usage_is_read() -> None:
    adapter, transport = _adapter(_ok("{}"))
    response = adapter.summarize(REQUEST)
    url, payload = transport.calls[0]
    assert url == "https://models.invalid/v1/chat/completions"
    messages = cast("list[dict[str, str]]", payload["messages"])
    user = cast("dict[str, object]", json.loads(messages[-1]["content"]))
    assert set(user) == {"title", "source_name", "published_at", "content"}
    assert response.usage == {"input_tokens": 7, "output_tokens": 3}
    assert adapter.identity.prompt_version == "openai-summary-prompt.v1"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (TransportTimeoutError(), SummaryFailureCode.PROVIDER_TIMEOUT),
        (TransportConnectionError(), SummaryFailureCode.PROVIDER_UNAVAILABLE),
        (TransportResponse(503, b"down"), SummaryFailureCode.PROVIDER_UNAVAILABLE),
        (TransportResponse(429, b""), SummaryFailureCode.PROVIDER_UNAVAILABLE),
        (TransportResponse(401, b"no"), SummaryFailureCode.PROVIDER_REJECTED),
        (TransportResponse(302, b""), SummaryFailureCode.PROVIDER_REJECTED),
        (TransportResponse(200, b"not json"), SummaryFailureCode.MALFORMED_OUTPUT),
    ],
)
def test_failures_map_to_stable_codes(
    response: TransportResponse | Exception, code: SummaryFailureCode
) -> None:
    adapter, _ = _adapter(response)
    with pytest.raises(SummaryProviderError) as caught:
        _ = adapter.summarize(REQUEST)
    assert caught.value.code is code
    assert caught.value.__cause__ is None


def test_unconfigured_provider_never_produces_a_summary() -> None:
    with pytest.raises(SummaryProviderError) as caught:
        _ = UnconfiguredSummarizer().summarize(REQUEST)
    assert caught.value.code is SummaryFailureCode.PROVIDER_REJECTED


MALFORMED_200: list[object] = [
    {"choices": [None]},
    {"choices": [{"finish_reason": "stop"}]},
    {"choices": [{"message": None, "finish_reason": "stop"}]},
    {"choices": [{"message": {"content": 7}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
    {"choices": []},
    [],
]


@pytest.mark.parametrize("body", MALFORMED_200)
def test_malformed_200_bodies_are_typed_malformed_output(body: object) -> None:
    adapter, _ = _adapter(TransportResponse(200, json.dumps(body).encode()))
    with pytest.raises(SummaryProviderError) as caught:
        _ = adapter.summarize(REQUEST)
    assert caught.value.code is SummaryFailureCode.MALFORMED_OUTPUT


@pytest.mark.parametrize("body", MALFORMED_200[:3])
def test_malformed_response_fails_the_job_and_the_worker_continues(
    body: object,
) -> None:
    queue = MemoryQueue()
    repo = MemorySummaries(queue)
    first, second = (
        repo.add(),
        repo.add("Một bài khác. Có câu thứ hai dài hơn tám ký tự."),
    )
    for source in (first, second):
        _ = queue.add(JobKind.SUMMARY, source.article_id, source.content_hash)
    adapter, _ = _adapter(TransportResponse(200, json.dumps(body).encode()))
    runner = SummaryRunner(SummarySteps(repo, adapter, queue))

    report = runner.run(5, "w")

    assert [o.status for o in report.outcomes] == ["failed", "failed"]
    assert {j.failure_code for j in queue.jobs.values()} == {"malformed_output"}
    assert {j.status for j in queue.jobs.values()} == {"FAILED"}
