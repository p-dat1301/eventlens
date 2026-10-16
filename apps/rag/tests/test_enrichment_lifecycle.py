from itertools import product
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from eventlens_contracts import EnrichmentStatus
from eventlens_rag.application.enrichment import bundled_retry_policy
from eventlens_rag.domain.enrichment_lifecycle import (
    LEGAL_TRANSITIONS,
    EnrichmentIdentity,
    FailureCode,
    IllegalTransitionError,
    RetryPolicy,
    require_transition,
)

S = EnrichmentStatus
POLICY_FILE = (
    Path(__file__).parents[1] / "src/eventlens_rag/policies/enrichment-retry.v1.json"
)
EXPECTED_LEGAL = {
    (S.PENDING, S.PROCESSING),
    (S.PENDING, S.STALE),
    (S.PROCESSING, S.PROCESSING),
    (S.PROCESSING, S.READY),
    (S.PROCESSING, S.PENDING),
    (S.PROCESSING, S.FAILED),
    (S.PROCESSING, S.STALE),
    (S.READY, S.STALE),
    (S.FAILED, S.STALE),
    (S.STALE, S.PENDING),
}


def test_legal_transition_table_is_exactly_the_documented_lifecycle() -> None:
    assert set(LEGAL_TRANSITIONS) == EXPECTED_LEGAL
    for source, target in EXPECTED_LEGAL:
        require_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    sorted(set(product(S, S)) - EXPECTED_LEGAL),
)
def test_every_other_transition_is_forbidden(
    source: EnrichmentStatus, target: EnrichmentStatus
) -> None:
    with pytest.raises(IllegalTransitionError):
        require_transition(source, target)


def test_versioned_policy_fixture_fixes_budget_deadline_lease_backoff_and_pass() -> (
    None
):
    raw = TypeAdapter(dict[str, object]).validate_json(POLICY_FILE.read_bytes())
    policy = bundled_retry_policy()
    assert raw["policy_id"] == policy.policy_id == "enrichment-retry.v1"
    assert policy.max_attempts == 3
    assert policy.attempt_timeout_seconds == 30
    assert policy.lease_seconds == 120
    assert policy.max_jobs_per_pass == 25
    assert [policy.backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 120, 480, 600]
    assert set(policy.retryable_failure_codes) == {
        FailureCode.PROVIDER_TIMEOUT,
        FailureCode.PROVIDER_UNAVAILABLE,
    }


def test_policy_retries_transport_inside_budget_then_exhausts() -> None:
    policy = bundled_retry_policy()
    first = policy.decide(1, FailureCode.PROVIDER_TIMEOUT)
    second = policy.decide(2, FailureCode.PROVIDER_UNAVAILABLE)
    last = policy.decide(3, FailureCode.PROVIDER_TIMEOUT)
    assert (first.retry_in_seconds, first.exhausted) == (30, False)
    assert (second.retry_in_seconds, second.exhausted) == (120, False)
    assert (last.retry_in_seconds, last.exhausted) == (None, True)


@pytest.mark.parametrize(
    "code",
    [
        FailureCode.MALFORMED_OUTPUT,
        FailureCode.UNSUPPORTED_TAXONOMY,
        FailureCode.UNGROUNDED_EVIDENCE,
        FailureCode.UNGROUNDED_METADATA,
        FailureCode.INVALID_METADATA,
        FailureCode.PROVIDER_REFUSAL,
        FailureCode.PROVIDER_REJECTED,
        FailureCode.INVALID_STAGE,
    ],
)
def test_validation_failures_never_retry_blindly(code: FailureCode) -> None:
    decision = bundled_retry_policy().decide(1, code)
    assert decision.retry_in_seconds is None
    assert not decision.exhausted


def test_policy_rejects_lease_shorter_than_two_deadlines() -> None:
    payload = bundled_retry_policy().model_dump(mode="json")
    payload["lease_seconds"] = 59
    with pytest.raises(ValidationError, match="lease_shorter_than_deadline"):
        _ = RetryPolicy.model_validate_json(TypeAdapter(object).dump_json(payload))


def test_model_or_prompt_change_alone_creates_a_new_identity_key() -> None:
    article = uuid4()
    base = EnrichmentIdentity("article-enrichment.v1", "m1", "p1")
    keys = {
        base.key_for(article, "sha256:" + "0" * 64),
        EnrichmentIdentity("article-enrichment.v1", "m2", "p1").key_for(
            article, "sha256:" + "0" * 64
        ),
        EnrichmentIdentity("article-enrichment.v1", "m1", "p2").key_for(
            article, "sha256:" + "0" * 64
        ),
    }
    assert len(keys) == 3
