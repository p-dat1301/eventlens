"""Phase-1 guards for the frozen T6 interfaces and policy artifacts."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from eventlens_rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_synthesis_policy,
)
from eventlens_rag.domain.model_input import (
    SYNTHESIS_INSTRUCTIONS,
    SYNTHESIS_PROMPT_VERSION,
    ModelInput,
    SourceArticlePayload,
    SourceSpanPayload,
    serialize_model_input,
)
from eventlens_rag.domain.synthesis_draft import parse_draft
from eventlens_rag.domain.synthesis_errors import (
    MalformedOutputReason,
    SynthesisMalformedOutputError,
)

_OBJECT = TypeAdapter(dict[str, object])
_ROWS = TypeAdapter(list[dict[str, object]])
SPAN = UUID("00000000-0000-4000-8000-000000000001")


def _draft(**overrides: object) -> str:
    body: dict[str, object] = {
        "schema_version": "synthesis_draft.v1",
        "evidence_status": "sufficient",
        "claims": [
            {
                "claim_ref": "c1",
                "text": "Bridge closed.",
                "evidence_span_ids": [str(SPAN)],
                "temporal_claim": None,
            }
        ],
        "uncertainties": [],
    }
    body.update(overrides)
    return json.dumps(body)


def test_bundled_policies_load_with_frozen_fixture_numbers() -> None:
    synthesis = bundled_synthesis_policy()
    activation = bundled_activation_policy()
    assert synthesis.label == "fixture"
    assert synthesis.budget.max_input_tokens_per_request == 12_000
    assert synthesis.budget.max_output_tokens_per_request == 1_500
    assert synthesis.budget.max_articles == 20
    assert synthesis.concurrency.max_concurrent_requests == 30
    assert synthesis.deadlines.request_deadline_seconds == 20.0
    assert synthesis.deadlines.call_timeout_seconds == 15.0
    assert activation.mode == "fixture"
    assert activation.budget.synthesis_policy_id == synthesis.policy_id
    assert synthesis.prompt_version == SYNTHESIS_PROMPT_VERSION


def test_strict_draft_rejects_extra_fields_and_invalid_json() -> None:
    assert parse_draft(_draft()).claims[0].claim_ref == "c1"
    with pytest.raises(SynthesisMalformedOutputError) as extra:
        _ = parse_draft(_draft(source_reference="https://evil.example/"))
    assert extra.value.reason is MalformedOutputReason.SCHEMA_VIOLATION
    with pytest.raises(SynthesisMalformedOutputError) as broken:
        _ = parse_draft("{not json")
    assert broken.value.reason is MalformedOutputReason.INVALID_JSON


def test_serialized_input_counts_schema_and_carries_only_allowlisted_fields() -> None:
    source = SourceArticlePayload(
        article_id=UUID("00000000-0000-4000-8000-0000000000aa"),
        title="Title",
        source_name="Wire",
        source_domain="wire.example",
        source_published_at=datetime(2026, 10, 1, tzinfo=UTC),
        spans=(SourceSpanPayload(span_id=SPAN, text="Bridge closed."),),
    )
    serialized = serialize_model_input(
        ModelInput(
            prompt_version=SYNTHESIS_PROMPT_VERSION,
            instructions=SYNTHESIS_INSTRUCTIONS,
            question="What closed?",
            sources=(source,),
        )
    )
    user = _OBJECT.validate_json(serialized.messages[1].content)
    sources = _ROWS.validate_python(user["sources"])
    assert set(sources[0]) == {
        "article_id",
        "title",
        "source_name",
        "source_domain",
        "source_published_at",
        "spans",
    }
    assert set(_ROWS.validate_python(sources[0]["spans"])[0]) == {"span_id", "text"}
    assert serialized.sent_span_ids == frozenset({SPAN})
    assert set(_OBJECT.validate_json(serialized.counted_text)) == {
        "messages",
        "response_format",
        "tools",
    }
    assert "synthesis_draft" in serialized.counted_text
