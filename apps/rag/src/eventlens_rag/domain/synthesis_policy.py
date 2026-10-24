"""Versioned synthesis/budget policy model (D7/D32, frozen interface).

The bundled ``synthesis-policy.v1.json`` is the single numeric source for the
runtime and the tests. Literal fields freeze semantics: changing behavior
requires a new policy version, never a reinterpretation of the same field.
"""

# ruff: noqa: EM101

from typing import Annotated, Literal, LiteralString, Self, get_args

from pydantic import Field, PositiveInt, model_validator
from pydantic_core import PydanticCustomError

from eventlens_contracts.base import ContractModel, NonEmptyText

HARD_MAX_ARTICLES = 20
HARD_MAX_INPUT_TOKENS = 12_000
HARD_MAX_OUTPUT_TOKENS = 1_500
HARD_MAX_CONCURRENCY = 30

ModelReasonCode = Literal[
    "insufficient_evidence",
    "conflicting_sources",
    "ambiguous_question",
    "temporal_unclear",
]
"""Reason codes a provider draft may state (no free-form prose)."""

ServerReasonCode = Literal[
    "no_matches",
    "no_query_terms",
    "empty_publication_range",
    "filter_unavailable",
    "dropped_at_final_check",
    "partial_filter_coverage",
]
"""Reason codes only the server may emit (retrieval/coverage outcomes)."""

UncertaintyCategory = Literal[
    "insufficient_evidence",
    "ambiguity",
    "source_conflict",
    "temporal_uncertainty",
]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class BudgetLimits(ContractModel):
    """Cumulative per-user-request limits shared by every call/retry."""

    max_articles: Annotated[int, Field(ge=1, le=HARD_MAX_ARTICLES)]
    max_input_tokens_per_request: Annotated[int, Field(ge=1, le=HARD_MAX_INPUT_TOKENS)]
    max_output_tokens_per_request: Annotated[
        int, Field(ge=1, le=HARD_MAX_OUTPUT_TOKENS)
    ]
    output_reservation_per_call: Annotated[int, Field(ge=1, le=HARD_MAX_OUTPUT_TOKENS)]
    max_model_calls_per_request: Literal[1]
    max_retries: Literal[0]
    framing_reserve_tokens: Annotated[int, Field(ge=0, le=2_000)]
    min_evidence_tokens: PositiveInt


class CountingPolicy(ContractModel):
    """What counts as model input and how it is serialized before counting."""

    serialization: Literal["synthesis-input-json.v1"]
    counted_parts: tuple[
        Literal[
            "system_instructions",
            "question",
            "user_framing",
            "evidence_sources",
            "extra_messages",
            "tool_definitions",
            "response_format_schema",
            "message_wrappers",
        ],
        ...,
    ]
    character_counts_allowed: Literal[False]
    unknown_tokenizer_in_live: Literal["fail_closed"]


class LedgerPolicy(ContractModel):
    """Request-scoped ledger settlement rules (no refunds that enable retries)."""

    scope: Literal["user_request"]
    reserve_before_call: Literal[True]
    reset_on_retry_or_error: Literal[False]
    settle_input: Literal["max_of_local_count_and_valid_reported"]
    settle_output_on_success: Literal["valid_reported_else_full_reservation"]
    settle_on_failure: Literal["charge_full_reservation"]
    drift_record: Literal["reported_minus_local_input_tokens"]


class DeadlinePolicy(ContractModel):
    """Fixture deadline/timeout numbers (not SLOs; p95 8s stays a target)."""

    unit: Literal["seconds"]
    request_deadline_seconds: Annotated[float, Field(gt=0, le=120)]
    call_timeout_seconds: Annotated[float, Field(gt=0, le=120)]
    call_timeout_rule: Literal["min(call_timeout_seconds, control.remaining())"]
    p95_latency_target_seconds: Annotated[float, Field(gt=0)]
    p95_is_slo: Literal[False]


class ConcurrencyPolicy(ContractModel):
    """Process-local admission; not a distributed limit."""

    max_concurrent_requests: Annotated[int, Field(ge=1, le=HARD_MAX_CONCURRENCY)]
    scope: Literal["process_local_shared_limiter"]
    overflow: Literal["reject_typed_never_queue"]
    in_flight_after_cancel: Literal["slot_held_until_call_returns"]


class FixturePricing(ContractModel):
    """Fixture rates for admission/accounting only; never measured cost."""

    basis: Literal["fixture"]
    unit: Literal["fixture_micro_usd"]
    input_per_token: Annotated[int, Field(ge=0)]
    output_per_token: Annotated[int, Field(ge=0)]
    per_call_fee: Annotated[int, Field(ge=0)]
    request_cost_cap: PositiveInt
    note: NonEmptyText


class UncertaintyTemplate(ContractModel):
    """Server-owned explanation text for one reason code."""

    reason_code: ModelReasonCode | ServerReasonCode
    category: UncertaintyCategory
    text: NonEmptyText


class SynthesisPolicy(ContractModel):
    """Frozen synthesis/budget policy (fixture label)."""

    schema_version: Literal["synthesis_policy.v1"]
    policy_id: NonEmptyText
    label: Literal["fixture"]
    rationale: NonEmptyText
    prompt_version: NonEmptyText
    draft_schema_version: Literal["synthesis_draft.v1"]
    template_version: NonEmptyText
    budget: BudgetLimits
    counting: CountingPolicy
    ledger: LedgerPolicy
    deadlines: DeadlinePolicy
    concurrency: ConcurrencyPolicy
    pricing: FixturePricing
    uncertainty_templates: tuple[UncertaintyTemplate, ...]

    @model_validator(mode="after")
    def enforce_consistency(self) -> Self:
        """Reject self-contradictory numbers or missing templates."""
        if (
            self.budget.output_reservation_per_call
            > self.budget.max_output_tokens_per_request
        ):
            raise _error(
                "output_reservation_exceeds_cap",
                "per-call output reservation must not exceed the request cap",
            )
        if (
            self.deadlines.call_timeout_seconds
            > self.deadlines.request_deadline_seconds
        ):
            raise _error(
                "call_timeout_exceeds_deadline",
                "call timeout must not exceed the request deadline",
            )
        codes = [template.reason_code for template in self.uncertainty_templates]
        if len(set(codes)) != len(codes):
            raise _error("duplicate_template", "template reason codes must be unique")
        required = set(get_args(ModelReasonCode)) | set(get_args(ServerReasonCode))
        if set(codes) != required:
            raise _error(
                "template_coverage", "every reason code needs exactly one template"
            )
        return self

    def template(self, reason_code: str) -> UncertaintyTemplate:
        """Return the template for a reason code (KeyError if unknown)."""
        for template in self.uncertainty_templates:
            if template.reason_code == reason_code:
                return template
        raise KeyError(reason_code)
