"""Typed T6 synthesis failures with stable codes (frozen interface, phase 1).

Every error exposes a class-level ``code`` plus, where useful, one enum
``reason``. Messages are fixed strings: they never carry provider bodies,
prompts, article text, endpoints, credentials or Authorization headers.
Failures are exceptions, never empty answers or abstentions (spec section 8).
"""

from enum import StrEnum
from typing import ClassVar


class BudgetFailureReason(StrEnum):
    """Why a budget admission refused work (zero provider call)."""

    CONTEXT_TOO_SMALL = "context_too_small"
    HEADER_EXCEEDS_INPUT = "header_exceeds_input"
    INPUT_CAP = "input_cap"
    OUTPUT_CAP = "output_cap"
    COST_CAP = "cost_cap"
    CALL_CAP = "call_cap"
    EVIDENCE_EXCEEDS_BUDGET = "evidence_exceeds_budget"


class GroundingFailureCode(StrEnum):
    """Mechanical citation/claim linkage failures (not semantic support)."""

    UNKNOWN_SPAN = "unknown_span"
    SPAN_NOT_SENT = "span_not_sent"
    DUPLICATE_CLAIM_REF = "duplicate_claim_ref"
    DUPLICATE_SPAN_REF = "duplicate_span_ref"
    DANGLING_CLAIM_REF = "dangling_claim_ref"
    WRONG_ARTICLE = "wrong_article"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    OFFSET_MISMATCH = "offset_mismatch"
    EXCERPT_MISMATCH = "excerpt_mismatch"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    TEMPORAL_EVIDENCE_NOT_CITED = "temporal_evidence_not_cited"
    CLAIM_WITHOUT_CITATION = "claim_without_citation"
    STATUS_MISMATCH = "status_mismatch"
    ANSWER_CONTRACT_INVALID = "answer_contract_invalid"


class MalformedOutputReason(StrEnum):
    """Why the provider's structured content failed the strict draft schema."""

    INVALID_JSON = "invalid_json"
    SCHEMA_VIOLATION = "schema_violation"


class ProviderFailureCode(StrEnum):
    """Stable provider-boundary codes produced by synthesis adapters."""

    TIMEOUT = "provider_timeout"
    UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "provider_rate_limited"
    HTTP_STATUS = "provider_http_status"
    REFUSAL = "provider_refusal"
    TRUNCATED = "provider_truncated"
    INVALID_JSON = "provider_invalid_json"
    BAD_SHAPE = "provider_bad_shape"
    BAD_FINISH = "provider_bad_finish"
    MODEL_MISMATCH = "provider_model_mismatch"
    REDIRECT_REJECTED = "provider_redirect_rejected"


class ActivationFailureCode(StrEnum):
    """Fail-closed reasons for provider activation (D31)."""

    POLICY_MISSING = "policy_missing"
    POLICY_INVALID = "policy_invalid"
    UNTRUSTED_ISSUER = "untrusted_issuer"
    NOT_APPROVED = "not_approved"
    ENDPOINT_INVALID = "endpoint_invalid"
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    ENDPOINT_CREDENTIALS = "endpoint_credentials"
    ENDPOINT_FRAGMENT = "endpoint_fragment"
    INSECURE_SCHEME = "insecure_scheme"
    TLS_VERIFICATION_DISABLED = "tls_verification_disabled"
    PROXY_NOT_ALLOWED = "proxy_not_allowed"
    MODEL_MISMATCH = "model_mismatch"
    TOKENIZER_MISMATCH = "tokenizer_mismatch"
    CORPUS_MISMATCH = "corpus_mismatch"
    FIELDS_MISMATCH = "fields_mismatch"
    BUDGET_POLICY_MISMATCH = "budget_policy_mismatch"
    COST_POLICY_MISSING = "cost_policy_missing"
    API_KEY_MISSING = "api_key_missing"


class SynthesisError(RuntimeError):
    """Base typed synthesis failure; never converted into an abstention."""

    code: ClassVar[str] = "synthesis_failed"


class SynthesisCancelledError(SynthesisError):
    """Shared cancellation observed; no further DB/model work, result dropped."""

    code: ClassVar[str] = "synthesis_cancelled"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("synthesis cancelled")


class SynthesisDeadlineExceededError(SynthesisError):
    """End-to-end request deadline elapsed."""

    code: ClassVar[str] = "synthesis_deadline_exceeded"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("synthesis deadline exceeded")


class SynthesisAccessDeniedError(SynthesisError):
    """Trusted caller scope is not (or no longer) authorized; nothing leaks."""

    code: ClassVar[str] = "synthesis_access_denied"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("synthesis access denied")


class SynthesisStorageError(SynthesisError):
    """Current-source storage unavailable; never an empty result."""

    code: ClassVar[str] = "synthesis_storage_unavailable"

    def __init__(self) -> None:
        """Hide connection details."""
        super().__init__("synthesis storage unavailable")


class SynthesisConfigurationError(SynthesisError):
    """Policy/tokenizer/model wiring is inconsistent (fail closed)."""

    code: ClassVar[str] = "synthesis_configuration_invalid"


class SynthesisBudgetError(SynthesisError):
    """Token, output, cost or call admission refused; zero provider call."""

    code: ClassVar[str] = "synthesis_budget_exceeded"

    def __init__(self, reason: BudgetFailureReason) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: BudgetFailureReason = reason


class SynthesisConcurrencyRejectedError(SynthesisError):
    """Process-local concurrency limit reached; the request is not queued."""

    code: ClassVar[str] = "synthesis_concurrency_rejected"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("synthesis concurrency limit reached")


class SynthesisEvidenceStaleError(SynthesisError):
    """Evidence changed (correction/tombstone/eligibility) at a final check."""

    code: ClassVar[str] = "synthesis_evidence_stale"

    def __init__(self) -> None:
        """Fixed message."""
        super().__init__("synthesis evidence no longer current")


class SynthesisMalformedOutputError(SynthesisError):
    """Provider content failed the strict draft schema; no raw fallback."""

    code: ClassVar[str] = "synthesis_malformed_output"

    def __init__(self, reason: MalformedOutputReason) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: MalformedOutputReason = reason


class SynthesisGroundingError(SynthesisError):
    """Draft failed mechanical claim/citation validation; no raw fallback."""

    code: ClassVar[str] = "synthesis_grounding_failed"

    def __init__(self, reason: GroundingFailureCode) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: GroundingFailureCode = reason


class SynthesisProviderError(SynthesisError):
    """Provider/transport failure mapped to a stable code; no body retained."""

    code: ClassVar[str] = "synthesis_provider_failed"

    def __init__(self, reason: ProviderFailureCode) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: ProviderFailureCode = reason


class ProviderActivationError(SynthesisError):
    """Activation policy rejected the provider configuration (zero requests)."""

    code: ClassVar[str] = "provider_activation_rejected"

    def __init__(self, reason: ActivationFailureCode) -> None:
        """Expose only the reason code."""
        super().__init__(reason.value)
        self.reason: ActivationFailureCode = reason
