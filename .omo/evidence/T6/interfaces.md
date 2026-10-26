# T6 frozen interfaces (phase 1)

Status: FROZEN on 2026-10-18 for phase 2 parallel work. Base commit
`45051be669a95651d66eb701a725daf05cca41e3` (T5), branch `feature/rag-synthesis-t6`,
worktree `.claude/worktrees/rag-synthesis-t6`. Spec:
`docs/tasks/t6-grounded-synthesis.md` (main repo). Nothing is committed.

Rules for every phase 2 worker:

- Do not edit any **shared frozen file** (section 1). If you need an interface
  change, stop and report the exact change and the reason. Do not work around it
  with a local copy of a type.
- Edit only files in your ownership list (section 5). Do not edit `conftest.py`,
  `Makefile`, `cli.py`, `README`, `pyproject.toml`, `uv.lock`, migrations
  001–006 or `retrieval-policy.v1/v2/v3.json`, `enrichment-retry.v1.json`.
- Domain, application and ports must not import LangGraph, FastAPI, psycopg,
  typer, an SDK, `eventlens_rag.adapters` or `eventlens_rag.graph`
  (`test_architecture.py`). No network, no live provider, no commit.
- Shell commands use the `rtk` prefix (`/Users/phungdinhdat/.codex/RTK.md`).
  `uv` needs the sandbox disabled because of its cache directory.
- The numbers come from the bundled policies. Load them with
  `bundled_synthesis_policy()` and `bundled_activation_policy()`. Do not
  hardcode a second copy in runtime modules. Tests may build variant
  policies with `SynthesisPolicy.model_validate(...)` only for negative cases.

## 1. Shared frozen files

| File | Contents |
| --- | --- |
| `apps/rag/src/eventlens_rag/domain/synthesis_errors.py` | Typed errors and reason enums |
| `apps/rag/src/eventlens_rag/domain/synthesis_policy.py` | `SynthesisPolicy` model and reason-code literals |
| `apps/rag/src/eventlens_rag/domain/synthesis_draft.py` | Strict draft schema, `parse_draft`, `draft_json_schema` |
| `apps/rag/src/eventlens_rag/domain/model_input.py` | Allowlisted payload DTOs, instructions, `serialize_model_input` |
| `apps/rag/src/eventlens_rag/domain/synthesis.py` | Result, usage, provenance, ledger, plan and registry DTOs |
| `apps/rag/src/eventlens_rag/domain/activation.py` | `ActivationPolicy` and endpoint/env/decision types |
| `apps/rag/src/eventlens_rag/ports/synthesis.py` | Model, ledger, lease/limiter, source reader, telemetry and trust ports |
| `apps/rag/src/eventlens_rag/application/synthesis_policies.py` | Bundled policy loaders |
| `apps/rag/src/eventlens_rag/policies/synthesis-policy.v1.json` | Budget/deadline/concurrency/pricing/templates (fixture) |
| `apps/rag/src/eventlens_rag/policies/provider-activation.fixture.v1.json` | D31 activation artifact (fixture) |
| `apps/rag/tests/test_synthesis_contracts.py` | Phase 1 guards (lead) |

Reused unchanged from T1–T5: `TokenCounter`/`TokenizerIdentity`
(`ports/tokens.py`), `FixtureTokenCounter` (`adapters/fixture_tokens.py`),
`QueryControl` and the `Retrieval*` types (`domain/retrieval.py`),
`RetrievalStore.current_articles` / `CurrentArticleState` (`ports/retrieval.py`),
`AccessPolicy` (`ports/access.py`), `CallerScope`, `BriefingQuery`,
`BriefingAnswer`/`Claim`/`Citation`/`Uncertainty`/`DisplayBlock`, `TemporalClaim`,
`ArticleDocument`, `EvidenceSpan`, `EmbeddingPort`/`EmbeddingVector`.
`RetrieveEvidence` and `RetrievalOptions` are unchanged.

## 2. Exact signatures (shared)

### 2.1 Errors (`domain/synthesis_errors.py`)

All errors subclass `SynthesisError(RuntimeError)` and have a class-level
`code: ClassVar[str]` and fixed messages. Errors that take an argument
store the enum value on `.reason`. No error ever carries provider bodies,
prompts, article text, endpoints or keys.

| Class | `code` | `.reason` enum |
| --- | --- | --- |
| `SynthesisCancelledError()` | `synthesis_cancelled` | – |
| `SynthesisDeadlineExceededError()` | `synthesis_deadline_exceeded` | – |
| `SynthesisAccessDeniedError()` | `synthesis_access_denied` | – |
| `SynthesisStorageError()` | `synthesis_storage_unavailable` | – |
| `SynthesisConfigurationError(msg)` | `synthesis_configuration_invalid` | – |
| `SynthesisBudgetError(reason)` | `synthesis_budget_exceeded` | `BudgetFailureReason`: `context_too_small`, `header_exceeds_input`, `input_cap`, `output_cap`, `cost_cap`, `call_cap`, `evidence_exceeds_budget` |
| `SynthesisConcurrencyRejectedError()` | `synthesis_concurrency_rejected` | – |
| `SynthesisEvidenceStaleError()` | `synthesis_evidence_stale` | – |
| `SynthesisMalformedOutputError(reason)` | `synthesis_malformed_output` | `MalformedOutputReason`: `invalid_json`, `schema_violation` |
| `SynthesisGroundingError(reason)` | `synthesis_grounding_failed` | `GroundingFailureCode`: `unknown_span`, `span_not_sent`, `duplicate_claim_ref`, `duplicate_span_ref`, `dangling_claim_ref`, `wrong_article`, `content_hash_mismatch`, `offset_mismatch`, `excerpt_mismatch`, `fingerprint_mismatch`, `temporal_evidence_not_cited`, `claim_without_citation`, `status_mismatch`, `answer_contract_invalid` |
| `SynthesisProviderError(reason)` | `synthesis_provider_failed` | `ProviderFailureCode`: `provider_timeout`, `provider_unavailable`, `provider_rate_limited`, `provider_http_status`, `provider_refusal`, `provider_truncated`, `provider_invalid_json`, `provider_bad_shape`, `provider_bad_finish`, `provider_model_mismatch`, `provider_redirect_rejected` |
| `ProviderActivationError(reason)` | `provider_activation_rejected` | `ActivationFailureCode`: `policy_missing`, `policy_invalid`, `untrusted_issuer`, `not_approved`, `endpoint_invalid`, `endpoint_not_allowed`, `endpoint_credentials`, `endpoint_fragment`, `insecure_scheme`, `tls_verification_disabled`, `proxy_not_allowed`, `model_mismatch`, `tokenizer_mismatch`, `corpus_mismatch`, `fields_mismatch`, `budget_policy_mismatch`, `cost_policy_missing`, `api_key_missing` |

`provider_invalid_json` is used when the *outer HTTP body* is not JSON (adapter).
If the outer body is valid but the structured *content* does not parse, the
application raises `SynthesisMalformedOutputError` from `parse_draft`.

### 2.2 Strict draft (`domain/synthesis_draft.py`, schema `synthesis_draft.v1`)

```python
class DraftClaim(ContractModel):          # extra=forbid, strict, frozen
    claim_ref: str                        # ^c[1-9][0-9]?$  (local label, not an ID)
    text: NonEmptyText                    # max 1000 chars
    evidence_span_ids: tuple[UUID, ...]   # 1..8, unique
    temporal_claim: TemporalClaim | None  # T1 contract, reused
class DraftUncertainty(ContractModel):
    reason_code: ModelReasonCode          # insufficient_evidence|conflicting_sources|ambiguous_question|temporal_unclear
    claim_ref: str | None
class SynthesisDraft(ContractModel):
    schema_version: Literal["synthesis_draft.v1"]
    evidence_status: Literal["sufficient", "insufficient"]
    claims: tuple[DraftClaim, ...]        # <= 20
    uncertainties: tuple[DraftUncertainty, ...]  # <= 8
def draft_json_schema() -> dict[str, object]
def parse_draft(content: str) -> SynthesisDraft   # raises SynthesisMalformedOutputError (from None)
```

Shape rules enforced at runtime: claim refs are unique, span IDs within a
claim are unique, (reason, claim_ref) pairs are unique, there are no
dangling uncertainty claim refs, `sufficient` requires at least one claim,
and `insufficient` requires zero claims plus an `insufficient_evidence`
reason. The draft has no free-form uncertainty prose, title, URL, hash,
version or scope field. The server assigns `claim_id`, `citation_id` and
`uncertainty_id`. Uncertainty text comes from the policy templates
(`template_version`).

### 2.3 Model input and serialization (`domain/model_input.py`)

```python
SERIALIZATION = "synthesis-input-json.v1"; SYNTHESIS_PROMPT_VERSION = "synthesis-prompt.v1"
SYNTHESIS_INSTRUCTIONS: str               # trusted system message (versioned)
@dataclass SourceSpanPayload(span_id: UUID, text: str)
@dataclass SourceArticlePayload(article_id, title, source_name, source_domain,
                                source_published_at: datetime | None,
                                spans: tuple[SourceSpanPayload, ...])
@dataclass ChatMessage(role: Literal["system","user","assistant","tool"], content: str)
@dataclass ModelInput(prompt_version, instructions, question,
                      sources: tuple[SourceArticlePayload, ...],
                      extra_messages: tuple[ChatMessage, ...] = (),
                      tool_definitions: tuple[str, ...] = ())   # canonical JSON strings
@dataclass SerializedModelInput(serialization, prompt_version,
                      messages: tuple[ChatMessage, ...], response_format_json: str,
                      tool_definitions: tuple[str, ...], counted_text: str,
                      sent_span_ids: frozenset[UUID])
def source_payload(article: RetrievedArticle, spans: tuple[RetrievedSpan, ...]) -> SourceArticlePayload
def response_format() -> dict[str, object]  # {"type":"json_schema","json_schema":{"name":"synthesis_draft","strict":true,"schema":...}}
def serialize_model_input(model_input: ModelInput) -> SerializedModelInput
```

Messages are `[system: instructions, user: compact sorted JSON
{"question","sources"}, *extra_messages]`. `counted_text` is the compact
sorted JSON `{"messages":[{role,content}...],"response_format":{...},"tools":[...]}`,
so instructions, question, framing, evidence, schema and wrappers are all
counted. The provider adapter must send exactly `serialized.messages` and
`json.loads(serialized.response_format_json)`. Source spans are the T5
`RetrievedSpan.span` after truncation (ID/excerpt as given) and never a span
rebuilt from the document.

### 2.4 Use-case DTOs (`domain/synthesis.py`)

```python
class SynthesisOutcome(StrEnum): ANSWERED | ABSTAINED_NO_EVIDENCE | ABSTAINED_BY_MODEL
class UsageStatus(StrEnum): NOT_CALLED | REPORTED | MISSING | INVALID
@dataclass ProviderUsage(input_tokens: int, output_tokens: int)
@dataclass ChatModelIdentity(model_id: str, model_version: str)
@dataclass LedgerReservation(reservation_id: int, input_tokens: int, output_tokens: int, cost: int)
@dataclass LedgerSnapshot(tokenizer, admissions, input_charged, output_charged, cost_charged,
                          input_open, output_open, cost_open,
                          remaining_input, remaining_output, remaining_cost,
                          input_drift_tokens: int | None)
@dataclass SynthesisPlan(remaining_context_budget: int, header_tokens: int,
                         framing_reserve_tokens: int, output_reservation: int,
                         tokenizer: TokenizerIdentity, synthesis_policy_id: str)
@dataclass SelectedArticle(article: RetrievedArticle, spans: tuple[RetrievedSpan, ...])
@dataclass TrustedSource(document: ArticleDocument, sent_spans: tuple[EvidenceSpan, ...])
@dataclass TrustedSourceRegistry(sources: Mapping[UUID, TrustedSource], span_owner: Mapping[UUID, UUID])
    def lookup(self, span_id: UUID) -> tuple[TrustedSource, EvidenceSpan] | None
@dataclass GroundedDraft(claims: tuple[Claim,...], citations: tuple[Citation,...],
                         uncertainties: tuple[Uncertainty,...],
                         evidence_status: Literal["sufficient","insufficient"])
@dataclass SynthesisUsage(model_calls, input_tokens_counted, input_tokens_charged,
                          output_tokens_reserved, output_tokens_charged,
                          provider_input_tokens: int|None, provider_output_tokens: int|None,
                          usage_status: UsageStatus, input_drift_tokens: int|None,
                          cost_charged: int, cost_unit: str)
@dataclass SynthesisProvenance(synthesis_policy_id, activation_policy_id,
                               mode: Literal["fixture","live"], chat_model: ChatModelIdentity,
                               tokenizer: TokenizerIdentity, prompt_version, draft_schema_version,
                               template_version, serialization, retrieval_policy_id,
                               index_version, corpus_generation)
@dataclass SynthesisMetrics(stages: tuple[StageMetric,...], total_latency_ms: float,
                            articles_sent, spans_sent, claims, citations, source_batch_reads)
@dataclass SynthesisResult(outcome: SynthesisOutcome, answer: BriefingAnswer,
                           abstention_reason: str | None, coverage: FilterCoverage,
                           usage: SynthesisUsage, provenance: SynthesisProvenance,
                           metrics: SynthesisMetrics)
@dataclass SynthesisTelemetryEvent(outcome: str, reason: str | None,
                                   usage: SynthesisUsage | None,
                                   provenance: SynthesisProvenance | None,
                                   metrics: SynthesisMetrics | None)
```

`TrustedSource.document` must be built by a validated construction of the
current `ArticleDocument` with `evidence_spans = sent spans`, for example
`ArticleDocument.model_validate({**doc.model_dump(), "evidence_spans": ...})`
in strict-compatible form. Never use `model_copy(update=...)`, and never fake
hashes. `BriefingAnswer.source_documents` is internal. T7 maps the public DTO.

### 2.5 Ports (`ports/synthesis.py`)

```python
@dataclass SynthesisRequest(serialized: SerializedModelInput, max_output_tokens: int, timeout_seconds: float)
@dataclass SynthesisResponse(content: str, usage: ProviderUsage | None,
                             usage_status: UsageStatus, reported_model: str | None)
class SynthesisModelPort(Protocol):
    @property identity -> ChatModelIdentity
    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse   # 1 call, no retry; raises SynthesisProviderError only
class TokenLedger(Protocol):
    @property identity -> TokenizerIdentity
    def count(self, serialized: SerializedModelInput) -> int
    def reserve(self, serialized: SerializedModelInput, output_tokens: int) -> LedgerReservation  # atomic; SynthesisBudgetError
    def settle(self, reservation, outcome: Literal["success","failure"], usage: ProviderUsage | None) -> None
    def snapshot(self) -> LedgerSnapshot
class AdmissionLease(Protocol):
    @property released -> bool
    def pin(self) -> Callable[[], None]     # idempotent unpin; slot held while pinned
    def release(self) -> None               # idempotent
class ConcurrencyLimiter(Protocol):
    @property capacity -> int; @property in_use -> int
    def try_acquire(self) -> AdmissionLease # SynthesisConcurrencyRejectedError; never queues
class CurrentArticleReader(Protocol):       # RetrievalStore satisfies it structurally
    def current_articles(self, article_ids: tuple[UUID, ...]) -> tuple[CurrentArticleState, ...]
class SynthesisTelemetry(Protocol):
    def emit(self, event: SynthesisTelemetryEvent) -> None
class TrustedIssuerRegistry(Protocol):
    def approves(self, issuer: str, policy_id: str, policy_sha256: str) -> bool
```

### 2.6 Activation types (`domain/activation.py`)

`ActivationPolicy` (`provider_activation_policy.v1`) has these fields:
`policy_id`, `issuer`, `mode: "fixture"|"live"`, `label` (must equal mode),
`rationale`, `endpoints: tuple[EndpointRule,...]`, and
`EndpointRule(scheme="https", host, port, base_path)`. Hosts are exact
lowercase names with no wildcard. It also has `chat_model` and
`embedding_model`, both `ModelBinding(role, model_id, model_version,
tokenizer_id, tokenizer_version, tokenizer_basis: fixture|exact|
conservative_estimate, safety_margin_ratio, embedding_dimension:
1536|None)`, plus `corpus: CorpusPolicy`, whose `allowed_source_fields`
equals the frozen five groups, and `prohibited`. The remaining fields are
`transport: TransportPolicy` (exact HTTP contract: `/chat/completions`,
`/embeddings`, POST, roles system/user, `json_schema_strict`, `max_tokens`,
temperature 0, the usage mapping, finish `stop`, no redirects, TLS verify,
no env proxy, 0 client retries), `budget: BudgetBinding(synthesis_policy_id,
source="synthesis_policy")` and `cost: CostApproval(basis, pricing_source)`.

Data-only helpers:

```python
@dataclass CanonicalEndpoint(scheme: Literal["https"], host: str, port: int, base_path: str)
    def url(self, path: str) -> str
@dataclass ApiKey(value: str)              # repr/str redacted
@dataclass ProviderEnvironment(base_url: str|None, api_key: ApiKey|None,
                               chat_model: str|None, embedding_model: str|None)
@dataclass ActivationDecision(mode, policy_id, issuer, chat_model: ModelBinding,
                              embedding_model: ModelBinding,
                              endpoint: CanonicalEndpoint|None, api_key: ApiKey|None)
```

### 2.7 Loaders (`application/synthesis_policies.py`)

`bundled_synthesis_policy() -> SynthesisPolicy`,
`bundled_activation_policy() -> ActivationPolicy`,
`bundled_activation_policy_sha256() -> str` (hex digest of the exact bytes).

## 3. Signatures to implement in phase 2 (frozen names/constructors)

### Worker A (core)

```python
# application/ledger.py
class RequestTokenLedger:                   # implements TokenLedger; thread-safe (Lock)
    def __init__(self, counter: TokenCounter, policy: SynthesisPolicy) -> None
# application/concurrency.py
class ProcessConcurrencyLimiter:            # implements ConcurrencyLimiter; one per runtime
    def __init__(self, capacity: int) -> None   # composition passes policy.concurrency.max_concurrent_requests
# application/synthesize.py
@dataclass(frozen=True, slots=True)
class PrepareSynthesis:
    tokens: TokenCounter
    policy: SynthesisPolicy
    def execute(self, query: BriefingQuery, ledger: TokenLedger) -> SynthesisPlan
        # raises SynthesisBudgetError(HEADER_EXCEEDS_INPUT | CONTEXT_TOO_SMALL); no DB/model work
@dataclass(frozen=True, slots=True)
class SynthesizeAnswer:
    model: SynthesisModelPort
    sources: CurrentArticleReader
    access: AccessPolicy
    tokens: TokenCounter
    policy: SynthesisPolicy
    activation: ActivationDecision
    telemetry: SynthesisTelemetry | None = None
    ids: Callable[[], UUID] = uuid4
    timer: Callable[[], float] = time.perf_counter
    def execute(self, query: BriefingQuery, retrieval: RetrievalResult, caller: CallerScope,
                control: QueryControl, ledger: TokenLedger, lease: AdmissionLease) -> SynthesisResult
# application/citations.py
def select_evidence(retrieval: RetrievalResult, max_articles: int) -> tuple[SelectedArticle, ...]
def build_registry(selected: tuple[SelectedArticle, ...], sent_span_ids: frozenset[UUID],
                   current: tuple[CurrentArticleState, ...]) -> TrustedSourceRegistry
        # raises SynthesisEvidenceStaleError when any sent article is not current/hash differs/tombstoned
def validate_draft(draft: SynthesisDraft, registry: TrustedSourceRegistry,
                   policy: SynthesisPolicy, ids: Callable[[], UUID]) -> GroundedDraft
        # raises SynthesisGroundingError(code)
# application/rendering.py
def render_answer(grounded: GroundedDraft, registry: TrustedSourceRegistry, *,
                  server_uncertainties: tuple[Uncertainty, ...], retrieved_at: datetime,
                  model_version: str, index_version: str) -> BriefingAnswer
def abstention_answer(reason_code: ServerReasonCode | ModelReasonCode, policy: SynthesisPolicy, *,
                      ids: Callable[[], UUID], retrieved_at: datetime,
                      model_version: str, index_version: str) -> BriefingAnswer
# adapters/fake_synthesis.py
class DeterministicFakeSynthesizer:         # SynthesisModelPort; cites first sent span per article
    def __init__(self, identity: ChatModelIdentity) -> None
# adapters/scripted_synthesis.py
class ScriptedSynthesizer:                  # SynthesisModelPort; returns/raises scripted items, records requests
    def __init__(self, identity: ChatModelIdentity,
                 script: Sequence[SynthesisResponse | SynthesisError | Callable[[SynthesisRequest], SynthesisResponse]]) -> None
    requests: list[SynthesisRequest]
```

Helper names inside A's modules (`select_evidence`, `build_registry`,
`validate_draft`, `render_answer`, `abstention_answer`) can be refined by A,
as long as A records the final names. T7-facing names are frozen:
`PrepareSynthesis`, `SynthesizeAnswer`, `RequestTokenLedger`,
`ProcessConcurrencyLimiter`, their constructors and `execute` signatures.

### Worker B (provider boundary)

```python
# application/activation.py   (framework-free; no network)
def read_provider_environment(environ: Mapping[str, str]) -> ProviderEnvironment
def canonicalize_endpoint(url: str) -> CanonicalEndpoint      # ProviderActivationError(endpoint_*/insecure_scheme)
def evaluate_activation(policy: ActivationPolicy, policy_sha256: str, synthesis: SynthesisPolicy,
                        environment: ProviderEnvironment, requested_mode: Literal["fixture","live"],
                        trust: TrustedIssuerRegistry,
                        counter_identity: TokenizerIdentity) -> ActivationDecision
        # fixture: always fixture decision, env ignored (key never enables live)
        # live: fail closed (ProviderActivationError) on any mismatch; zero requests
# adapters/trust.py
class NoTrustedIssuers:                     # TrustedIssuerRegistry; default, approves nothing
# adapters/openai_synthesis.py
class SynthesisTransport(Protocol):         # injected; must not retry/follow redirects/use env proxy
    def post_json(self, url: str, headers: Mapping[str, str], payload: Mapping[str, object],
                  timeout_seconds: float) -> TransportResponse
@dataclass TransportResponse(status: int, body: object, location: str | None)
def build_chat_payload(serialized: SerializedModelInput, model: str, max_output_tokens: int) -> dict[str, object]
class OpenAICompatibleSynthesizer:          # SynthesisModelPort
    def __init__(self, transport: SynthesisTransport, decision: ActivationDecision,
                 policy: ActivationPolicy) -> None
# adapters/redaction.py
def safe_error_fields(error: SynthesisError) -> dict[str, str]   # {"code", "reason"} only
# adapters/openai_embeddings.py  (optional; implements T3 EmbeddingPort, 1536 dims, finite values)
```

B owns the exact transport/response shape and may refine the names in its
own adapter files. `OpenAICompatibleSynthesizer` must satisfy
`SynthesisModelPort`. The adapter sets `max_tokens` from
`request.max_output_tokens` and uses `request.timeout_seconds` as the
transport timeout. It maps `prompt_tokens` to input and
`completion_tokens` to output, and returns `usage_status=INVALID` for a
negative, non-int or missing-field usage. It maps every failure to a
`ProviderFailureCode`.

## 4. Behavior contract (both workers build against this)

### 4.1 T7 call order (process-local; T7 owns composition)

```text
lease  = limiter.try_acquire()                         # one shared limiter; 31st -> SynthesisConcurrencyRejectedError
try:
  control = QueryControl(policy.deadlines.request_deadline_seconds)   # shared deadline/cancel
  ledger  = RequestTokenLedger(counter, policy)        # same counter instance as RetrieveEvidence.tokens
  plan    = PrepareSynthesis(counter, policy).execute(query, ledger)
  result  = RetrieveEvidence(...).execute(query, caller, control,
              RetrievalOptions(remaining_context_budget=plan.remaining_context_budget))
  answer  = SynthesizeAnswer(...).execute(query, result, caller, control, ledger, lease)
finally:
  lease.release()                                      # slot stays held while a pinned call is in flight
```

`plan.remaining_context_budget = min(query.context_budget,
ledger.snapshot().remaining_input - header_tokens - framing_reserve_tokens)`.
Here `header_tokens = ledger.count(serialize_model_input(ModelInput(...,
sources=())))`, which covers instructions, question, schema and wrappers. If
the header alone does not fit, the result is `HEADER_EXCEEDS_INPUT`. If the
remainder is below `min_evidence_tokens`, the result is
`CONTEXT_TOO_SMALL`. Both happen before retrieval and make zero calls.

In `SynthesizeAnswer`, the preflight serialization is rechecked with the
ledger immediately before the call. If it does not fit, A drops whole
trailing spans or articles. It never cuts text or JSON, and it does not
re-truncate. Only spans still in `serialized.sent_span_ids` may be cited. If
no span fits, the result is `SynthesisBudgetError(EVIDENCE_EXCEEDS_BUDGET)`
with zero calls. The call timeout is `min(call_timeout_seconds,
control.remaining())`. The call runs pinned (`unpin = lease.pin()`) and is
unpinned only when the call returns. If the caller is cancelled or times out,
it gets `SynthesisCancelledError` or `SynthesisDeadlineExceededError`, and
the late result is dropped. `control.admit()` runs before every DB/provider
step. A `Retrieval{Cancelled,DeadlineExceeded}Error` from `admit` is
re-raised as the `Synthesis*` equivalent.

Access is checked with `access.allows(caller)` at entry, after the
post-model batch read, and at the return boundary of **every** path,
including abstentions. Denial raises `SynthesisAccessDeniedError`, and no
result, counts or coverage are returned.

There is exactly one batched `sources.current_articles(ids)` call before the
provider call and one after it. Batched reads mean no N+1 queries, and no
transaction is held across the call.

### 4.2 Ledger settlement (from policy `ledger`)

`reserve` counts `counted_text`, then checks all of the following at once:
`input_charged + input_open + n <= 12000`, `output_charged + output_open +
out <= 1500`, and `cost_charged + cost_open + n*1 + out*4 + 2000 <= 20000`.
It checks in this order and raises at the first failure: `INPUT_CAP`,
`OUTPUT_CAP`, `COST_CAP`. `settle(success, usage)` charges input as
`max(local, reported)` and output as `reported` (if usage is valid) or else
the full reservation. Cost is recomputed from the charged tokens plus the
fee. Drift is recorded as `reported_input - local`. `settle(failure)`
charges the full reservation. There are no refunds or resets, and no ledger
call cap. The service enforces `max_model_calls_per_request = 1`
(`CALL_CAP`).

### 4.3 Outcome mapping (spec section 8; frozen)

| RetrievalResult / event | T6 result |
| --- | --- |
| `status=EMPTY`, `empty_reason` in `NO_MATCHES`, `NO_QUERY_TERMS`, `EMPTY_PUBLICATION_RANGE`, `DROPPED_AT_FINAL_CHECK` | `SynthesisResult(outcome=ABSTAINED_NO_EVIDENCE, abstention_reason=<same code lowercased>)`, server-rendered template, **zero model calls**, final access check |
| `status=EMPTY`, `empty_reason=FILTER_UNAVAILABLE` | `ABSTAINED_NO_EVIDENCE`, `abstention_reason="filter_unavailable"`, zero calls; the filter is never dropped |
| `status=EMPTY`, `empty_reason=CONTEXT_BUDGET_TOO_SMALL` | raise `SynthesisBudgetError(CONTEXT_TOO_SMALL)`, never an abstention |
| `status=EVIDENCE`, `coverage.status` in `PARTIAL`, `FILTER_UNAVAILABLE` | Proceed. The answer carries a server uncertainty `partial_filter_coverage` (template). The filter is not widened |
| Evidence invalid at pre-call or post-call batch check (correction/hash change/tombstone/eligibility change) | raise `SynthesisEvidenceStaleError`; whole answer fails; spans are not reused and scope is not widened |
| Storage failure in `current_articles` (`RetrievalStorageError`) | raise `SynthesisStorageError` |
| Access denied (entry/post-read/return) | raise `SynthesisAccessDeniedError` |
| Cancel / deadline | raise `SynthesisCancelledError` / `SynthesisDeadlineExceededError` (never provider failure/abstention/retry) |
| Budget refusal (header/context/input/output/cost/call) | raise `SynthesisBudgetError(reason)`, zero calls |
| Provider timeout/transport/status/refusal/truncation/shape | raise `SynthesisProviderError(code)`; no retry; no raw text |
| Content not valid `SynthesisDraft` | raise `SynthesisMalformedOutputError(reason)`; no repair/retry |
| Draft fails mechanical linkage | raise `SynthesisGroundingError(code)`; no partial answer |
| Valid draft `evidence_status=insufficient` | `SynthesisResult(outcome=ABSTAINED_BY_MODEL, abstention_reason="insufficient_evidence")`, no claims, template uncertainty |
| Valid grounded draft | `SynthesisResult(outcome=ANSWERED)`; `answer.answer == rendered_answer()` from display blocks (claims in draft order, then uncertainties) |
| Limiter full | `SynthesisConcurrencyRejectedError`, raised by `try_acquire` before any work |

A no-call abstention still returns a valid `BriefingAnswer` with
`model_version = activation.chat_model.model_version`,
`index_version = retrieval.provenance.index_version`,
`retrieved_at = retrieval.provenance.query_time`, `source_documents=()` and
`usage.usage_status = NOT_CALLED`.

## 5. File ownership for phase 2

| Owner | Files (create/edit) |
| --- | --- |
| **Worker A, core** | `application/synthesize.py`, `application/ledger.py`, `application/concurrency.py`, `application/citations.py`, `application/rendering.py`, `adapters/fake_synthesis.py`, `adapters/scripted_synthesis.py`; tests `test_synthesis.py`, `test_citations.py`, `test_synthesis_budget.py`, `test_synthesis_ledger.py`, `test_synthesis_concurrency.py`, helper `tests/synthesis_support.py` |
| **Worker B, provider boundary** | `application/activation.py`, `adapters/openai_synthesis.py`, `adapters/trust.py`, `adapters/redaction.py`, optional `adapters/openai_embeddings.py`; tests `test_openai_adapter.py`, `test_redaction.py`, `test_activation.py`, `test_activation_endpoint.py`, optional `test_openai_embeddings.py`, helper `tests/provider_support.py` |
| **Lead (phase 3)** | `tests/test_synthesis_db.py` (real PostgreSQL), `tests/test_synthesis_e2e.py`, `adapters/synthesis_smoke.py`, `cli.py` (`synthesis-smoke`), `Makefile`, `apps/rag/README.md`, `.omo/plans/eventlens-ai-rag-mvp.md`, `.omo/evidence/**`, `tests/test_synthesis_contracts.py`, any composition wiring |
| **Shared frozen (nobody edits in phase 2)** | the files in section 1, `conftest.py`, `pyproject.toml`, `uv.lock`, all existing T1–T5 sources/tests/policies/migrations |

Each worker logs its runs under `.omo/evidence/task-6-logs/run-<UTC>-<A|B>/`
with `exit-codes.tsv`. Each worker runs `ruff format --check`, `ruff check`,
`basedpyright` on its own files, and `pytest apps/rag/tests -q` without a
DSN (DB tests skip) before handing back.

## 6. Ambiguities resolved in phase 1

1. **Cost cap reachability.** The fixture rates are 1 per input token, 4 per
   output token, a 2,000 fee per call, and a cap of 20,000. One call at the
   full 12,000/1,500 caps costs exactly 20,000, so the D7 single-call gate is
   reachable. Two calls whose cumulative tokens stay within the caps pay the
   fee twice and exceed the cap. That gives a cost cap that is independently
   testable with zero calls, without lowering the 12k gate.
2. **Single numeric source.** The activation artifact binds to
   `synthesis-policy.v1` (`budget.synthesis_policy_id`) instead of copying
   caps, deadline, timeout, concurrency, retries or pricing. A mismatch is
   `budget_policy_mismatch`.
3. **Approval.** A policy file never approves itself. Live mode needs
   `TrustedIssuerRegistry.approves(issuer, policy_id, sha256)` from
   composition. T6 ships `NoTrustedIssuers` only. Tests use a test registry.
4. **T5 vs T6 context format.** The T5 context text includes `content_hash`
   and offsets, which must not go to the provider. T6 never sends
   `EvidenceContext.text`. It rebuilds an allowlisted payload from
   `RetrievedArticle`/`RetrievedSpan`, recounts the full payload, and drops
   whole spans if needed (2.3, 4.1). `framing_reserve_tokens = 256` absorbs
   the framing differences.
5. **Uncertainty prose.** The model supplies only reason codes. The text
   comes from versioned templates in the policy. Server-only reasons
   (retrieval/coverage) are a separate literal set the model cannot emit.
6. **Errors vs results.** Failures are exceptions, as in T5. Abstentions are
   results.
7. **Deadline numbers** (fixture, seconds): the request deadline is 20 and
   the per-call timeout is 15, capped by the remaining time. The 8 s p95
   stays a target, not a timeout.
8. **Ledger has no call cap.** `max_model_calls_per_request=1` is enforced by
   the service, so ledger tests can simulate multiple admissions as the spec
   requires.

## 7. Phase 3 integration decisions (appended 2026-10-19; sections 1–6 unchanged)

Lead decisions on Worker A deviations and Worker B requests
(`.omo/evidence/T6/interface-requests-B.md`). No shared frozen file or policy
artifact was edited in phase 3. No policy needed a new version: the fixes
below add fail-closed checks and do not reinterpret any policy field.

| # | Item | Decision | Reason / action |
| --- | --- | --- | --- |
| A-1 | `build_registry(selected, sent_span_ids, current, query)` takes extra `query` | **ACCEPT** | Needed to recheck scope and metadata eligibility (domains, publication range, topic/entity filters) on the current revision after the call (spec §5). Final signature recorded here. |
| A-2 | `span_owner` also maps retrieved-but-unsent spans | **ACCEPT** | `lookup` still returns `None` for unsent spans, because only `sent_spans` are searched, so they can never be cited. The extra mapping lets the validator report `span_not_sent` instead of `unknown_span`. Covered in `test_citations.py`. |
| A-3 | `PrepareSynthesis` refuses `OUTPUT_CAP`/`COST_CAP` before retrieval | **ACCEPT** | Stricter than frozen: a zero-work refusal when no single call could be admitted. The reason codes come from the frozen `BudgetFailureReason`. |
| A-gap-1 | Late usage after cancel/timeout is not recorded | **OPEN (accepted limit)** | The full reservation is charged on failure, which is conservative. A provider overshoot reported after cancel is not reflected. Late-usage accounting goes to T7/T14 or live activation. |
| A-gap-2 | Model mismatch is charged before raising | **ACCEPT** | The call did happen. Charging it is correct, and there is no refund. |
| A-gap-3 | The fake reports usage `MISSING`, so the full output reservation is charged | **ACCEPT** | Fixture behavior, labelled. Reported-usage accounting is exercised with scripted and mock-transport responses. |
| B-1 | Live activation is unreachable because v1 pricing is fixture-only | **ACCEPT as intended fail-closed; OPEN for live** | T6 must not create live approval. A future `synthesis-policy.v2` with an `approved_live` pricing block plus a trusted issuer is required. Tests build `ActivationDecision` directly, labelled test-only. |
| B-2 | `evaluate_activation` has no expected-corpus input | **OPEN** | Changing the frozen signature is not needed for fixture scope. Binding the index corpus or generation to `corpus_ids` belongs to live activation (D31) and T7 composition. Recorded as a live-activation prerequisite. |
| B-3 | Ambient proxy/TLS env handled by extra helpers `activate`, `reject_ambient_overrides`, `endpoint_allowed` | **ACCEPT** | Composition (T7) must call `activate(...)`, not `evaluate_activation` directly. Added to the T7 interface list. |
| B-4 | A must settle the ledger on any `SynthesisError` from the port | **ACCEPT; FIXED** | `SynthesizeAnswer._run_pinned` caught only `SynthesisProviderError`, so `SynthesisConfigurationError`/`ProviderActivationError` were relabelled `provider_unavailable`. It now keeps every typed `SynthesisError` code, and the reservation is settled as a failure. Regression: `test_synthesis_integration.py::test_typed_non_provider_adapter_error_keeps_code_and_settles` (red before the fix, green after; logs in `run-20261019T001334Z-phase3-dev/02,03`). |
| B-5 | Strict payload guard (`[system,user]`, exact instructions, no tools) | **ACCEPT** | A already builds `ModelInput(instructions=SYNTHESIS_INSTRUCTIONS)` with no extras. Verified end to end by `test_live_adapter_composes_with_service_and_maps_usage`. |
| L-1 | Composition gap: B's adapter accepts a reported `model` equal to `model_id` **or** `model_version`; A rejected anything except `model_id` | **FIXED** | A now accepts the same set. Regression `test_provider_reporting_model_version_is_accepted_end_to_end` (red before, green after). |
| L-2 | `SynthesizeAnswer` did not check that the T5 context was counted with the ledger tokenizer | **FIXED (new fail-closed guard)** | Spec §7.2 requires T5 and T6 to share one `TokenCounter` mapping. An evidence result whose `context.tokenizer_id/version` differs from `ledger.identity` raises `SynthesisConfigurationError` with zero calls. Regression `test_retrieval_counted_with_another_tokenizer_is_rejected_before_call` (red before, green after). |

### 7.1 T7-facing interface (final)

- Composition order as in §4.1. For provider mode, call
  `activate(policy, sha256, synthesis_policy, os.environ, requested_mode,
  trust, counter.identity)`. Fixture mode returns the fixture decision. Live
  mode fails closed in T6 (B-1, B-2).
- `SynthesizeAnswer(model, sources, access, tokens, policy, activation,
  telemetry=None, ids=uuid4, timer=perf_counter).execute(query, retrieval,
  caller, control, ledger, lease) -> SynthesisResult`, or it raises a typed
  `SynthesisError`.
- `PrepareSynthesis(tokens, policy).execute(query, ledger) -> SynthesisPlan`.
- `RequestTokenLedger(counter, policy)`, `ProcessConcurrencyLimiter(capacity)`.
- `OpenAICompatibleSynthesizer(transport, decision, policy)`. It requires a
  live decision. The transport is injected; no HTTP client ships with the
  package.
- Smoke: `eventlens-rag synthesis-smoke [--output] [--evidence]` in
  `adapters/synthesis_smoke.py`, which follows the same order.
