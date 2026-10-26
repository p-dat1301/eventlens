"""T6 E2E on an isolated real DB (spec section 9.11).

T4 ingest -> lexical-only answer -> T4 fake enrichment (READY + vectors) ->
T5 filtered hybrid retrieval -> T6 fake synthesis -> validated cited answer;
an invalid model span is a typed failure with no raw fallback.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ledger import RequestTokenLedger
from backend.rag.application.ports.synthesis import SynthesisModelPort
from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    EnrichmentSteps,
    bundled_retry_policy,
)
from backend.rag.application.use_cases.index import EnsureGeneration
from backend.rag.application.use_cases.ingest import IngestArticle
from backend.rag.application.use_cases.retrieve import RetrievalOptions
from backend.rag.application.use_cases.synthesize import (
    PrepareSynthesis,
    SynthesizeAnswer,
)
from backend.rag.domain.embedding import EmbeddingIdentity
from backend.rag.domain.enrichment_lifecycle import EnrichmentIdentity, OutcomeKind
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import RetrievalMode
from backend.rag.domain.synthesis import SynthesisOutcome, SynthesisResult
from backend.rag.domain.synthesis_errors import (
    GroundingFailureCode,
    SynthesisGroundingError,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.diagnostics.synthesis_smoke import Case, Runtime, dump, run_smoke
from backend.rag.infra.embeddings.fake_embeddings import FakeEmbeddings
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from backend.rag.infra.postgres.staging import PostgresStaging
from backend.rag.infra.repositories.postgres_enrichment import (
    PostgresEnrichmentRepository,
)
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from backend.rag.infra.telemetry.telemetry import RecordingTelemetry
from backend.rag.workflows.ingestion.workflow import EnrichmentRunner
from backend.shared.contracts.article import Article
from retrieval_support import (
    CALLER,
    MutableAccess,
    briefing,
    control,
    service,
)
from synthesis_support import (
    COUNTER,
    DECISION,
    IDENTITY,
    SYNTH_POLICY,
    claim,
    draft,
    respond,
    sequential_ids,
)

BUNDLE = Path(__file__).parents[4] / "fixtures/article_bundle.v1.json"
QUESTION = "công viên"
_CASES = TypeAdapter(dict[str, dict[str, object]])


def _ingest(url: str) -> Article:
    source = TypeAdapter(dict[str, object]).validate_json(BUNDLE.read_bytes())
    upstream = Article.model_validate_json(
        TypeAdapter(object).dump_json(source["article"])
    )
    enricher = FakeArticleEnricher()
    identity = EnrichmentIdentity(
        ENRICHMENT_VERSION, enricher.model_version, enricher.prompt_version
    )
    receipt = IngestArticle(
        PostgresEnrichmentRepository(url),
        FixtureAccessPolicy(),
        identity,
        lambda: datetime.now(UTC),
    ).execute(upstream, CALLER)
    assert receipt.job_id is not None
    return upstream


def _enrich(url: str) -> None:
    steps = EnrichmentSteps(
        repository=PostgresEnrichmentRepository(url),
        enricher=FakeArticleEnricher(),
        generations=EnsureGeneration(
            PostgresStaging(url), FakeEmbeddings(), EmbeddingIdentity()
        ),
        policy=bundled_retry_policy(),
        clock=lambda: datetime.now(UTC),
    )
    report = EnrichmentRunner(steps, RecordingTelemetry()).run(5)
    assert [o.kind for o in report.outcomes] == [OutcomeKind.READY]


def _answer(
    url: str, model: SynthesisModelPort, query: BriefingQuery
) -> tuple[SynthesisResult, RetrievalMode]:
    ledger = RequestTokenLedger(COUNTER, SYNTH_POLICY)
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger)
    retrieval = service(PostgresRetrievalStore(url)).execute(
        query,
        CALLER,
        control(),
        RetrievalOptions(remaining_context_budget=plan.remaining_context_budget),
    )
    use_case = SynthesizeAnswer(
        model=model,
        sources=PostgresRetrievalStore(url),
        access=MutableAccess(),
        tokens=COUNTER,
        policy=SYNTH_POLICY,
        activation=DECISION,
        ids=sequential_ids(),
    )
    lease = ProcessConcurrencyLimiter(30).try_acquire()
    try:
        result = use_case.execute(query, retrieval, CALLER, control(), ledger, lease)
    finally:
        lease.release()
    return result, retrieval.retrieval_mode


def test_ingest_enrich_retrieve_synthesize_validated_cited_answer(
    retrieval_url: str,
) -> None:
    upstream = _ingest(retrieval_url)
    fake = DeterministicFakeSynthesizer(IDENTITY)

    lexical, lexical_mode = _answer(retrieval_url, fake, briefing(QUESTION))
    _enrich(retrieval_url)
    hybrid, hybrid_mode = _answer(
        retrieval_url, fake, briefing(QUESTION, topic_filters=("other",))
    )

    assert lexical_mode is RetrievalMode.LEXICAL_ONLY
    assert lexical.outcome is SynthesisOutcome.ANSWERED
    assert hybrid_mode is RetrievalMode.HYBRID
    assert hybrid.outcome is SynthesisOutcome.ANSWERED
    answer = hybrid.answer
    assert answer.answer == answer.rendered_answer()
    (citation,) = answer.citations
    (source,) = answer.source_documents
    assert citation.article_id == upstream.article_id == source.article_id
    assert str(citation.source_reference) == str(source.canonical_url)
    assert citation.source_title == source.title
    (span,) = (
        s for s in source.evidence_spans if s.span_id == citation.evidence_span_id
    )
    assert (
        source.normalized_content[span.start_offset : span.end_offset] == span.excerpt
    )
    assert hybrid.provenance.retrieval_policy_id == "retrieval-policy.v3"
    assert hybrid.provenance.synthesis_policy_id == SYNTH_POLICY.policy_id
    assert hybrid.provenance.mode == "fixture"


def test_invalid_model_span_on_real_db_is_typed_failure(retrieval_url: str) -> None:
    _ = _ingest(retrieval_url)
    bad = UUID("00000000-0000-4000-8000-00000000beef")
    content = draft([claim("c1", "RAW-PROSE-CANARY invented fact", [bad])])
    model = ScriptedSynthesizer(IDENTITY, [respond(content)])

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = _answer(retrieval_url, model, briefing(QUESTION))

    assert raised.value.reason is GroundingFailureCode.UNKNOWN_SPAN
    assert "RAW-PROSE-CANARY" not in str(raised.value)
    assert len(model.requests) == 1


def test_synthesis_smoke_asserts_every_case_on_real_db(retrieval_url: str) -> None:
    fixtures = Path(__file__).parents[4] / "fixtures/rag"
    report, evidence = run_smoke(retrieval_url, fixtures)

    assert report["all_passed"] is True
    cases = _CASES.validate_python(report["cases"])
    assert set(cases) == {
        "happy_cited_answer",
        "no_evidence_abstention",
        "lexical_only",
        "invalid_citation_blocked",
        "provider_timeout",
        "budget_failure",
        "revoked_access",
    }
    assert "RAW" not in dump(report)
    assert evidence["citations"]


def test_smoke_case_with_wrong_expectation_is_reported_failed(
    retrieval_url: str,
) -> None:
    runtime = Runtime(retrieval_url, ProcessConcurrencyLimiter(30))
    wrong = Case(
        "wrong",
        briefing("zzzqqq xylophonequartz", result_limit=5),
        lambda _: DeterministicFakeSynthesizer(IDENTITY),
        expected="answered",
    )

    observed = runtime.run(wrong)

    assert observed["outcome"] == "abstained_no_evidence"
    assert observed["passed"] is False
