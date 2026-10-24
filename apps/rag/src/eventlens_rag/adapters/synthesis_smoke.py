"""Deterministic T6 smoke: T5 retrieval -> T6 synthesis on an EMPTY isolated DB.

Composition mirrors the frozen T7 call order (shared limiter -> lease ->
request ledger -> ``PrepareSynthesis`` -> ``RetrieveEvidence`` with the
remaining context budget -> ``SynthesizeAnswer``). Only fakes/scripted model
ports are used; no transport, network or provider key is touched. Every case
asserts its expected outcome; ``run_smoke`` reports ``all_passed`` and the CLI
exits non-zero otherwise. The summary carries IDs, codes, counts, usage and
provenance only; claim text/excerpts go to a separate evidence mapping.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from eventlens_rag.adapters.fake_embeddings import FakeEmbeddings
from eventlens_rag.adapters.fake_synthesis import DeterministicFakeSynthesizer
from eventlens_rag.adapters.fixture_tokens import FixtureTokenCounter
from eventlens_rag.adapters.identity_reranker import IdentityReranker
from eventlens_rag.adapters.postgres_retrieval import PostgresRetrievalStore
from eventlens_rag.adapters.retrieval_smoke import (
    CALLER,
    QUERY_CLOCK,
    enrich_fixture_corpus,
    load_fixture_corpus,
    questions,
)
from eventlens_rag.adapters.scripted_synthesis import ScriptedSynthesizer
from eventlens_rag.application.concurrency import ProcessConcurrencyLimiter
from eventlens_rag.application.ledger import RequestTokenLedger
from eventlens_rag.application.retrieve import (
    RetrievalOptions,
    RetrieveEvidence,
    bundled_retrieval_policy,
)
from eventlens_rag.application.synthesis_policies import (
    bundled_activation_policy,
    bundled_synthesis_policy,
)
from eventlens_rag.application.synthesize import PrepareSynthesis, SynthesizeAnswer
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.activation import ActivationDecision
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.query import BriefingQuery
from eventlens_rag.domain.retrieval import QueryControl
from eventlens_rag.domain.synthesis import (
    ChatModelIdentity,
    SynthesisResult,
    UsageStatus,
)
from eventlens_rag.domain.synthesis_draft import DRAFT_SCHEMA_VERSION
from eventlens_rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisError,
    SynthesisProviderError,
)
from eventlens_rag.ports.synthesis import (
    SynthesisModelPort,
    SynthesisRequest,
    SynthesisResponse,
)

UNKNOWN_SPAN = UUID("00000000-0000-4000-8000-00000000dead")


@dataclass(slots=True)
class SmokeAccess:
    """Fixture access policy that a case can revoke mid-request."""

    revoked: bool = False

    def allows(self, scope: CallerScope) -> bool:
        """Fixture caller only, unless revoked."""
        return not self.revoked and scope.policy_id == "fixture-policy"


@dataclass(slots=True)
class CountingModel:
    """Wrap a model port and count calls (zero-call assertions)."""

    inner: SynthesisModelPort
    calls: int = 0

    @property
    def identity(self) -> ChatModelIdentity:
        """Delegate identity."""
        return self.inner.identity

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
        """Count, then delegate."""
        self.calls += 1
        return self.inner.synthesize(request)


@dataclass(frozen=True, slots=True)
class Case:
    """One smoke case: query, model factory and expected outcome/code."""

    name: str
    query: BriefingQuery
    model: Callable[[SmokeAccess], SynthesisModelPort]
    expected: str
    expected_reason: str | None = None
    expected_calls: int | None = None
    expected_mode: str | None = None


@dataclass(slots=True)
class Runtime:
    """One composition: shared limiter, policies and fixture decision."""

    url: str
    limiter: ProcessConcurrencyLimiter
    counter: FixtureTokenCounter = field(default_factory=FixtureTokenCounter)

    def run(self, case: Case) -> dict[str, object]:
        """Execute one request in T7 order and check the expectation."""
        policy = bundled_synthesis_policy()
        activation = bundled_activation_policy()
        decision = fixture_decision()
        access = SmokeAccess()
        model = CountingModel(case.model(access))
        lease = self.limiter.try_acquire()
        observed: dict[str, object]
        mode: str | None = None
        try:
            control = QueryControl(policy.deadlines.request_deadline_seconds)
            ledger = RequestTokenLedger(self.counter, policy)
            plan = PrepareSynthesis(self.counter, policy).execute(case.query, ledger)
            retrieval = self.retrieval().execute(
                case.query,
                CALLER,
                control,
                RetrievalOptions(
                    remaining_context_budget=plan.remaining_context_budget
                ),
            )
            mode = retrieval.retrieval_mode.value
            result = SynthesizeAnswer(
                model=model,
                sources=PostgresRetrievalStore(self.url),
                access=access,
                tokens=self.counter,
                policy=policy,
                activation=decision,
            ).execute(case.query, retrieval, CALLER, control, ledger, lease)
            observed = _summary(result)
        except SynthesisError as error:
            reason: object = getattr(error, "reason", None)
            observed = {
                "outcome": error.code,
                "reason": None if reason is None else str(reason),
            }
        finally:
            lease.release()
        observed["model_calls"] = model.calls
        observed["retrieval_mode"] = mode
        observed["activation_policy_id"] = activation.policy_id
        checks = [
            observed["outcome"] == case.expected,
            case.expected_reason is None or observed["reason"] == case.expected_reason,
            case.expected_calls is None or model.calls == case.expected_calls,
            case.expected_mode is None or mode == case.expected_mode,
        ]
        observed["expected"] = {
            "outcome": case.expected,
            "reason": case.expected_reason,
            "model_calls": case.expected_calls,
            "retrieval_mode": case.expected_mode,
        }
        observed["passed"] = all(checks)
        return observed

    def retrieval(self) -> RetrieveEvidence:
        """T5 service sharing this runtime's counter."""
        return RetrieveEvidence(
            store=PostgresRetrievalStore(self.url),
            embeddings=FakeEmbeddings(),
            embedding_identity=EmbeddingIdentity(),
            reranker=IdentityReranker(),
            tokens=self.counter,
            access=SmokeAccess(),
            policy=bundled_retrieval_policy(),
            clock=lambda: QUERY_CLOCK,
        )


def fixture_decision() -> ActivationDecision:
    """Fixture activation decision (no endpoint, no key, never live)."""
    activation = bundled_activation_policy()
    return ActivationDecision(
        mode="fixture",
        policy_id=activation.policy_id,
        issuer=activation.issuer,
        chat_model=activation.chat_model,
        embedding_model=activation.embedding_model,
        endpoint=None,
        api_key=None,
    )


def _identity() -> ChatModelIdentity:
    binding = bundled_activation_policy().chat_model
    return ChatModelIdentity(binding.model_id, binding.model_version)


def _summary(result: SynthesisResult) -> dict[str, object]:
    usage = result.usage
    provenance = result.provenance
    return {
        "outcome": result.outcome.value,
        "reason": result.abstention_reason,
        "claims": len(result.answer.claims),
        "citations": len(result.answer.citations),
        "cited_article_ids": sorted(
            {str(c.article_id) for c in result.answer.citations}
        ),
        "uncertainties": [u.category for u in result.answer.uncertainties],
        "coverage": result.coverage.status.value,
        "usage": {
            "model_calls": usage.model_calls,
            "input_tokens_counted": usage.input_tokens_counted,
            "input_tokens_charged": usage.input_tokens_charged,
            "output_tokens_reserved": usage.output_tokens_reserved,
            "output_tokens_charged": usage.output_tokens_charged,
            "usage_status": usage.usage_status.value,
            "cost_charged": usage.cost_charged,
            "cost_unit": usage.cost_unit,
        },
        "provenance": {
            "synthesis_policy_id": provenance.synthesis_policy_id,
            "activation_policy_id": provenance.activation_policy_id,
            "mode": provenance.mode,
            "chat_model": provenance.chat_model.model_version,
            "tokenizer": provenance.tokenizer.tokenizer_id,
            "tokenizer_version": provenance.tokenizer.version,
            "prompt_version": provenance.prompt_version,
            "retrieval_policy_id": provenance.retrieval_policy_id,
        },
        "metrics": {
            "articles_sent": result.metrics.articles_sent,
            "spans_sent": result.metrics.spans_sent,
            "source_batch_reads": result.metrics.source_batch_reads,
            "total_latency_ms": round(result.metrics.total_latency_ms, 3),
        },
    }


def _invalid_citation(_: SmokeAccess) -> SynthesisModelPort:
    content = json.dumps(
        {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "evidence_status": "sufficient",
            "claims": [
                {
                    "claim_ref": "c1",
                    "text": "An uncited invented claim.",
                    "evidence_span_ids": [str(UNKNOWN_SPAN)],
                    "temporal_claim": None,
                }
            ],
            "uncertainties": [],
        }
    )
    return ScriptedSynthesizer(
        _identity(),
        [SynthesisResponse(content, None, UsageStatus.MISSING, _identity().model_id)],
    )


def _timeout(_: SmokeAccess) -> SynthesisModelPort:
    # Stands for the adapter's mapping of a transport timeout (the HTTP path
    # itself is covered by test_openai_adapter / test_synthesis_integration).
    return ScriptedSynthesizer(
        _identity(), [SynthesisProviderError(ProviderFailureCode.TIMEOUT)]
    )


def _revoking(access: SmokeAccess) -> SynthesisModelPort:
    fake = DeterministicFakeSynthesizer(_identity())

    def answer(request: SynthesisRequest) -> SynthesisResponse:
        access.revoked = True  # revocation committed while the model runs
        return fake.synthesize(request)

    return ScriptedSynthesizer(_identity(), [answer])


def _fake(_: SmokeAccess) -> SynthesisModelPort:
    return DeterministicFakeSynthesizer(_identity())


def _query(text: str, **overrides: object) -> BriefingQuery:
    payload: dict[str, object] = {
        "schema_version": "briefing_query.v1",
        "query": text,
        "publication_time_basis": "source_published_at",
        "result_limit": 5,
        "context_budget": 12_000,
    }
    payload.update(overrides)
    return BriefingQuery.model_validate(payload)


def run_smoke(url: str, fixtures: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Run all cases; return (safe summary, evidence with fixture excerpts)."""
    documents = load_fixture_corpus(url, fixtures)
    question = questions(fixtures)[0].text
    runtime = Runtime(
        url,
        ProcessConcurrencyLimiter(
            bundled_synthesis_policy().concurrency.max_concurrent_requests
        ),
    )
    before = [
        Case(
            "lexical_only",
            _query(question),
            _fake,
            "answered",
            expected_calls=1,
            expected_mode="lexical_only",
        ),
        Case(
            "no_evidence_abstention",
            _query("zzzqqq xylophonequartz"),
            _fake,
            "abstained_no_evidence",
            expected_calls=0,
        ),
        Case(
            "budget_failure",
            _query(question, context_budget=8),
            _fake,
            "synthesis_budget_exceeded",
            "context_too_small",
            expected_calls=0,
        ),
    ]
    cases: dict[str, dict[str, object]] = {
        case.name: runtime.run(case) for case in before
    }
    enrich_fixture_corpus(url, fixtures, documents)
    after = [
        Case(
            "happy_cited_answer",
            _query(question),
            _fake,
            "answered",
            expected_calls=1,
            expected_mode="hybrid",
        ),
        Case(
            "invalid_citation_blocked",
            _query(question),
            _invalid_citation,
            "synthesis_grounding_failed",
            "unknown_span",
            expected_calls=1,
        ),
        Case(
            "provider_timeout",
            _query(question),
            _timeout,
            "synthesis_provider_failed",
            "provider_timeout",
            expected_calls=1,
        ),
        Case(
            "revoked_access",
            _query(question),
            _revoking,
            "synthesis_access_denied",
            expected_calls=1,
        ),
    ]
    cases.update({case.name: runtime.run(case) for case in after})
    passed = [result["passed"] is True for result in cases.values()]
    evidence = _evidence(runtime, _query(question))
    policy = bundled_synthesis_policy()
    report: dict[str, object] = {
        "label": "fixture",
        "synthesis_policy_id": policy.policy_id,
        "activation_policy_id": bundled_activation_policy().policy_id,
        "retrieval_policy_id": bundled_retrieval_policy().policy_id,
        "limiter_in_use_after": runtime.limiter.in_use,
        "cases": cases,
        "all_passed": all(passed) and runtime.limiter.in_use == 0,
    }
    return report, evidence


def _evidence(runtime: Runtime, query: BriefingQuery) -> dict[str, object]:
    """Reviewer artifact: the happy answer's claims and citations (fixture)."""
    policy = bundled_synthesis_policy()
    ledger = RequestTokenLedger(runtime.counter, policy)
    control = QueryControl(policy.deadlines.request_deadline_seconds)
    plan = PrepareSynthesis(runtime.counter, policy).execute(query, ledger)
    retrieval = runtime.retrieval().execute(
        query,
        CALLER,
        control,
        RetrievalOptions(remaining_context_budget=plan.remaining_context_budget),
    )
    lease = runtime.limiter.try_acquire()
    try:
        result = SynthesizeAnswer(
            model=DeterministicFakeSynthesizer(_identity()),
            sources=PostgresRetrievalStore(runtime.url),
            access=SmokeAccess(),
            tokens=runtime.counter,
            policy=policy,
            activation=fixture_decision(),
        ).execute(query, retrieval, CALLER, control, ledger, lease)
    finally:
        lease.release()
    answer = result.answer
    return {
        "label": "fixture excerpts for review; not provider output",
        "answer": answer.answer,
        "claims": [
            {"claim_id": str(c.claim_id), "text": c.text} for c in answer.claims
        ],
        "citations": [
            {
                "claim_id": str(c.claim_id),
                "article_id": str(c.article_id),
                "evidence_span_id": str(c.evidence_span_id),
                "source_title": c.source_title,
                "source_reference": str(c.source_reference),
            }
            for c in answer.citations
        ],
    }


def dump(report: dict[str, object]) -> str:
    """Stable JSON rendering."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=str)
