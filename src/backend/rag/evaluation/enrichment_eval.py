"""``make enrichment-eval``: fake enricher vs manual gold on the fixture corpus.

Runs the default fake enricher over all 30 fixture articles through the real
compiled ingestion graph (in-memory repository, injected clock), then scores
the published READY metadata against ``fixtures/rag/enrichments`` (manual
ground truth). Lifecycle cases come from ``enrichment_cases``. The deterministic
section is byte-stable across runs; wall-clock latency lives in ``measured``.
"""

from backend.rag.application.use_cases.enrichment import (
    ENRICHMENT_VERSION,
    bundled_retry_policy,
)
from backend.rag.domain.enrichment_lifecycle import OutcomeKind
from backend.rag.infra.llm.fake_enricher import FakeArticleEnricher
from backend.rag.infra.llm.fixture_tokens import FIXTURE_TOKENIZER

from .corpus import EVAL_CLOCK, EvalCorpus, corpus_versions
from .enrichment_cases import run_lifecycle_cases
from .enrichment_metrics import (
    LABEL_FIELDS,
    SET_FIELDS,
    abstention_metrics,
    calibration_metrics,
    evidence_metrics,
    hallucination_metrics,
    label_field,
    set_field,
    taxonomy_metrics,
)
from .enrichment_world import EnrichmentWorld, MeteredEnricher
from .guards import no_network
from .metrics import (
    Json,
    digest,
    latency_summary,
    ratio,
)

TOKENIZER = f"{FIXTURE_TOKENIZER.tokenizer_id}@{FIXTURE_TOKENIZER.version}"
LIMITATIONS = (
    "Synthetic 30-article corpus; fake keyword enricher; not live quality.",
    (
        "Gold entity/location lists are empty for every article except 021, "
        "so entity/location recall has a denominator of 1."
    ),
    "In-memory repository: lifecycle semantics only, never DB evidence.",
)


def evaluate(corpus: EvalCorpus) -> tuple[dict[str, Json], dict[str, Json]]:
    """Return (deterministic report, measured-environment report)."""
    with no_network() as attempts:
        world = EnrichmentWorld()
        world.persist(*corpus.articles)
        enricher = MeteredEnricher(FakeArticleEnricher())
        outcomes = world.drain(enricher)
        lifecycle, lifecycle_latency = run_lifecycle_cases(corpus)
    predicted = world.published()
    articles = {a.article_id: a for a in corpus.articles}
    kinds = sorted({o.kind.value for o in outcomes})
    tokens_out = [
        c.output_tokens for c in enricher.calls if c.output_tokens is not None
    ]
    deterministic: dict[str, Json] = {
        "schema_version": "enrichment_eval_report.v1",
        "versions": {
            **corpus_versions(corpus.root),
            "system_enrichment_version": ENRICHMENT_VERSION,
            "system_model_version": enricher.model_version,
            "system_prompt_version": enricher.prompt_version,
            "retry_policy": bundled_retry_policy().policy_id,
            "tokenizer": TOKENIZER,
            "clock": EVAL_CLOCK.isoformat(),
            "gold": "fixtures/rag/enrichments (manual-ground-truth.v1)",
        },
        "sample": {
            "articles": len(corpus.articles),
            "gold_articles": len(corpus.gold),
            "jobs_run": len(outcomes),
            "ready": sum(o.kind is OutcomeKind.READY for o in outcomes),
            "outcome_kinds": list(kinds),
        },
        "coverage": ratio(len(predicted), len(corpus.gold), "articles READY / gold"),
        "fields": {
            **{
                n: set_field(n, e, predicted, corpus.gold)
                for n, e in SET_FIELDS.items()
            },
            **{
                n: label_field(e, predicted, corpus.gold)
                for n, e in LABEL_FIELDS.items()
            },
        },
        "taxonomy_validity": taxonomy_metrics(predicted),
        "evidence_support": evidence_metrics(predicted, articles),
        "confidence_calibration": calibration_metrics(predicted, corpus.gold),
        "abstention": abstention_metrics(predicted, corpus.gold),
        "hallucination": hallucination_metrics(predicted, corpus.gold),
        "lifecycle_cases": lifecycle,
        "safety": {
            "enricher": type(enricher.inner).__name__,
            "network_connect_attempts": attempts.count,
        },
        "tokens": {
            "tokenizer": "fixture regex counter, not a provider tokenizer",
            "provider_calls": len(enricher.calls),
            "input_tokens_total": sum(c.input_tokens for c in enricher.calls),
            "output_tokens_total": sum(tokens_out),
            "output_tokens_unmeasured_calls": len(enricher.calls) - len(tokens_out),
        },
        "cost_estimate": {
            "value": None,
            "status": "NOT MEASURED",
            "reason": "no enrichment pricing policy; fake enricher cost is 0",
        },
        "limitations": list(LIMITATIONS),
    }
    deterministic["report_digest"] = digest(deterministic)
    measured: dict[str, Json] = {
        "status": "measured on this machine; not reproducible; not a production SLO",
        "provider_call_latency": latency_summary(
            [c.latency_ms for c in enricher.calls]
        ),
        "lifecycle_case_latency": latency_summary(lifecycle_latency),
    }
    return deterministic, measured
