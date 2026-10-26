"""Provider activation policy and endpoint types (D31, frozen interface).

Only data shapes live here. Parsing/canonicalizing URLs, trust checks and the
fail-closed decision are implemented by the provider-boundary worker in
``application/activation.py``. A policy file can never approve itself: live
mode additionally requires a trusted issuer registry injected by composition.
"""

# ruff: noqa: EM101

from dataclasses import dataclass, field
from typing import Annotated, Literal, LiteralString, Self, override

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from backend.shared.contracts.base import ContractModel, NonEmptyText

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HostName = Annotated[str, Field(max_length=253, pattern=rf"^{_LABEL}(?:\.{_LABEL})+$")]
BasePath = Annotated[str, Field(pattern=r"^(?:/[A-Za-z0-9._~-]+)*$")]

AllowedSourceField = Literal[
    "article_id",
    "title",
    "normalized_content_spans",
    "source_name_domain",
    "source_published_at",
]
ALLOWED_SOURCE_FIELDS: tuple[AllowedSourceField, ...] = (
    "article_id",
    "title",
    "normalized_content_spans",
    "source_name_domain",
    "source_published_at",
)

ActivationMode = Literal["fixture", "live"]


def _error(error_type: LiteralString, message: LiteralString) -> PydanticCustomError:
    return PydanticCustomError(error_type, message)


class EndpointRule(ContractModel):
    """One exact HTTPS allowlist entry: no wildcard, no substring host match."""

    scheme: Literal["https"]
    host: HostName
    port: Annotated[int, Field(ge=1, le=65_535)]
    base_path: BasePath


class ModelBinding(ContractModel):
    """Approved model identity and its tokenizer mapping."""

    role: Literal["chat", "embedding"]
    model_id: NonEmptyText
    model_version: NonEmptyText
    tokenizer_id: NonEmptyText
    tokenizer_version: NonEmptyText
    tokenizer_basis: Literal["fixture", "exact", "conservative_estimate"]
    safety_margin_ratio: Annotated[float, Field(ge=0, le=1)] | None
    embedding_dimension: Literal[1536] | None

    @model_validator(mode="after")
    def enforce_binding(self) -> Self:
        """Estimators need an explicit margin; dimension only for embeddings."""
        needs_margin = self.tokenizer_basis == "conservative_estimate"
        if needs_margin != (self.safety_margin_ratio is not None):
            raise _error(
                "safety_margin_basis",
                "safety_margin_ratio is required only for conservative estimators",
            )
        if (self.role == "embedding") != (self.embedding_dimension is not None):
            raise _error(
                "embedding_dimension_role",
                "embedding_dimension is required only for embedding bindings",
            )
        return self


class CorpusPolicy(ContractModel):
    """Corpus/classification and the five allowlisted provider field groups."""

    corpus_ids: tuple[NonEmptyText, ...]
    data_classification: Literal["internal"]
    allowed_source_fields: tuple[AllowedSourceField, ...]
    prohibited: tuple[
        Literal[
            "canonical_url",
            "raw_storage_uri",
            "raw_content",
            "content_hash",
            "span_fingerprint",
            "processing_metadata",
            "enrichment_metadata",
            "credentials",
            "caller_scope",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def enforce_allowlist(self) -> Self:
        """The allowlist is exactly the plan's five field groups."""
        if self.allowed_source_fields != ALLOWED_SOURCE_FIELDS:
            raise _error(
                "source_fields_mismatch",
                "allowed_source_fields must equal the frozen five-field allowlist",
            )
        return self


class TransportPolicy(ContractModel):
    """Exact supported OpenAI-compatible HTTP contract (no vendor claims)."""

    chat_path: Literal["/chat/completions"]
    embeddings_path: Literal["/embeddings"]
    method: Literal["POST"]
    roles: tuple[Literal["system", "user"], ...]
    response_format: Literal["json_schema_strict"]
    output_token_field: Literal["max_tokens"]
    temperature: Literal[0]
    usage_mapping: Literal["prompt_tokens->input,completion_tokens->output"]
    success_finish_reason: Literal["stop"]
    follow_redirects: Literal[False]
    tls_verify: Literal[True]
    trust_env_proxy: Literal[False]
    client_retries: Literal[0]


class BudgetBinding(ContractModel):
    """Caps, deadline, timeout, concurrency and fixture cost come from here."""

    synthesis_policy_id: NonEmptyText
    source: Literal["synthesis_policy"]


class CostApproval(ContractModel):
    """Which price basis is approved; fixture rates never become live prices."""

    basis: Literal["fixture", "approved_live"]
    pricing_source: Literal["synthesis_policy.pricing"]


class ActivationPolicy(ContractModel):
    """Versioned provider activation artifact (``mode`` is not an approval)."""

    schema_version: Literal["provider_activation_policy.v1"]
    policy_id: NonEmptyText
    issuer: NonEmptyText
    mode: ActivationMode
    label: Literal["fixture", "live"]
    rationale: NonEmptyText
    endpoints: tuple[EndpointRule, ...]
    chat_model: ModelBinding
    embedding_model: ModelBinding
    corpus: CorpusPolicy
    transport: TransportPolicy
    budget: BudgetBinding
    cost: CostApproval

    @model_validator(mode="after")
    def enforce_shape(self) -> Self:
        """Roles fixed; fixture policies cannot carry live-only claims."""
        if self.chat_model.role != "chat" or self.embedding_model.role != "embedding":
            raise _error("binding_role", "chat/embedding bindings have fixed roles")
        if self.label != self.mode:
            raise _error("label_mode", "label must equal mode")
        if self.mode == "fixture" and self.cost.basis != "fixture":
            raise _error("fixture_cost", "fixture policies use fixture cost basis")
        if self.mode == "live" and self.chat_model.tokenizer_basis == "fixture":
            raise _error(
                "live_fixture_tokenizer", "live mode cannot use a fixture tokenizer"
            )
        if len(set(self.endpoints)) != len(self.endpoints):
            raise _error("duplicate_endpoint", "endpoint rules must be unique")
        return self


@dataclass(frozen=True, slots=True)
class CanonicalEndpoint:
    """Parsed exact endpoint: lowercase host, explicit port, no trailing slash."""

    scheme: Literal["https"]
    host: str
    port: int
    base_path: str

    def url(self, path: str) -> str:
        """Join a frozen API path (e.g. ``/chat/completions``)."""
        return f"{self.scheme}://{self.host}:{self.port}{self.base_path}{path}"


@dataclass(frozen=True, slots=True)
class ApiKey:
    """Secret wrapper whose repr/str never reveal the value."""

    value: str = field(repr=False)

    @override
    def __str__(self) -> str:
        """Redacted."""
        return "ApiKey(***)"


@dataclass(frozen=True, slots=True)
class ProviderEnvironment:
    """The four plan env vars, read once by composition; never a live switch.

    ``EVENTLENS_OPENAI_BASE_URL``, ``EVENTLENS_OPENAI_API_KEY``,
    ``EVENTLENS_CHAT_MODEL``, ``EVENTLENS_EMBEDDING_MODEL``.
    """

    base_url: str | None
    api_key: ApiKey | None
    chat_model: str | None
    embedding_model: str | None


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    """Fail-closed outcome; live fields are set only for an approved live run."""

    mode: ActivationMode
    policy_id: str
    issuer: str
    chat_model: ModelBinding
    embedding_model: ModelBinding
    endpoint: CanonicalEndpoint | None
    api_key: ApiKey | None
