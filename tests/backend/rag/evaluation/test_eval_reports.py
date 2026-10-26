"""T9 eval harness: reproducible reports, honest denominators, safety gates.

No database and no network: the evals run on in-memory adapters through the
real compiled ingestion and briefing StateGraphs.
"""

import socket
from collections.abc import Iterator
from typing import cast

import pytest

from backend.rag.composition import compose_briefing, fixture_store
from backend.rag.evaluation.corpus import load_corpus
from backend.rag.evaluation.enrichment_eval import evaluate as enrichment_evaluate
from backend.rag.evaluation.gating import gate_failures
from backend.rag.evaluation.guards import NetworkBlockedError, no_network
from backend.rag.evaluation.metrics import Json, calibration, percentile, ratio
from backend.rag.evaluation.rag_eval import evaluate as rag_evaluate
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer

type Report = dict[str, Json]


@pytest.fixture(scope="module")
def enrichment() -> Report:
    return enrichment_evaluate(load_corpus())[0]


@pytest.fixture(scope="module")
def rag() -> Report:
    return rag_evaluate(load_corpus(), repeats=2)[0]


def rates(node: Json) -> Iterator[dict[str, Json]]:
    """Every rate object (has numerator, denominator and value) in a report."""
    if isinstance(node, dict):
        if {"numerator", "denominator", "value"} <= node.keys():
            yield node
        for value in node.values():
            yield from rates(value)
    elif isinstance(node, list):
        for value in node:
            yield from rates(value)


def fixture_report(request: pytest.FixtureRequest, name: str) -> Report:
    value: object = request.getfixturevalue(name)  # pyright: ignore[reportAny]
    assert isinstance(value, dict)
    return cast("Report", value)


def section(report: Report, *path: str) -> dict[str, Json]:
    node: Json = report
    for key in path:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, dict)
    return node


def test_enrichment_report_is_byte_reproducible(enrichment: Report) -> None:
    # When
    again = enrichment_evaluate(load_corpus())[0]

    # Then
    assert again == enrichment
    assert str(enrichment["report_digest"]).startswith("sha256:")


def test_rag_report_is_reproducible_and_repeats_agree(rag: Report) -> None:
    # When
    again = rag_evaluate(load_corpus(), repeats=2)[0]

    # Then
    assert again["report_digest"] == rag["report_digest"]
    assert section(rag, "sample", "repeat_consistent") == {
        "lexical": True,
        "hybrid": True,
        "ranking_lexical": True,
        "ranking_hybrid": True,
    }


@pytest.mark.parametrize("name", ["enrichment", "rag"])
def test_every_rate_carries_denominator_and_definition(
    name: str, request: pytest.FixtureRequest
) -> None:
    # Given
    report = fixture_report(request, name)

    # When
    found = list(rates(report))

    # Then
    assert found
    for rate in found:
        assert isinstance(rate["definition"], str)
        if rate["denominator"] == 0:
            assert rate["value"] is None
            assert rate["not_measurable"] == "denominator is 0"


def test_enrichment_reports_every_requested_field(enrichment: Report) -> None:
    # Then
    fields = section(enrichment, "fields")
    assert set(fields) == {
        "topic",
        "entity",
        "location",
        "content_type",
        "event_relevance",
    }
    for key in (
        "taxonomy_validity",
        "evidence_support",
        "confidence_calibration",
        "abstention",
        "hallucination",
        "lifecycle_cases",
        "tokens",
        "cost_estimate",
    ):
        assert key in enrichment
    assert section(enrichment, "cost_estimate")["status"] == "NOT MEASURED"
    assert section(enrichment, "sample")["articles"] == 30


def test_lexical_and_hybrid_share_graph_corpus_and_questions(rag: Report) -> None:
    # Given
    lexical = section(rag, "modes", "lexical", "per_question")
    hybrid = section(rag, "modes", "hybrid", "per_question")

    # Then: same 10 questions; only the index state differs.
    assert lexical.keys() == hybrid.keys()
    assert len(lexical) == 10

    def modes(records: dict[str, Json]) -> set[str]:
        """Retrieval modes of questions that found evidence."""
        return {
            str(section(records, q)["retrieval_mode"])
            for q in records
            if section(records, q)["route"] == "answered"
        }

    assert modes(lexical) == {"lexical_only"}
    # Q007 has no lexical hit in its scope; fake vectors still return its
    # allowed article, so that one answer is vector_only.
    assert "hybrid" in modes(hybrid)
    assert modes(hybrid) <= {"hybrid", "vector_only"}
    assert section(rag, "sample")["reindexed_articles"] == 30


def test_semantic_support_is_separate_from_structural_validity(rag: Report) -> None:
    # Given
    semantic = section(rag, "semantic_vs_structural")

    # Then: structure accepts labelled-unsupported claims; only labels see it.
    accepted = section(semantic, "structural_acceptance")
    supported = section(semantic, "semantic_support_among_accepted")
    assert accepted["denominator"] == 8
    assert isinstance(semantic["accepted_but_unsupported_or_contradicted"], int)
    assert semantic["accepted_but_unsupported_or_contradicted"] > 0
    assert supported["denominator"] == accepted["numerator"]
    assert section(semantic, "automatic_semantic_check")["status"] == "NOT MEASURED"


def test_enrichment_eval_runs_under_the_network_guard(enrichment: Report) -> None:
    # Then
    assert section(enrichment, "safety") == {
        "enricher": "FakeArticleEnricher",
        "network_connect_attempts": 0,
    }


def test_safety_section_proves_fake_default_no_checkpointer_no_network(
    rag: Report,
) -> None:
    # Then
    assert section(rag, "safety") == {
        "default_model": "DeterministicFakeSynthesizer",
        "activation_mode": "fixture",
        "graph_checkpointer": "None",
        "eval_model_identity": "fixture-chat-model",
        "network_connect_attempts": 0,
    }


@pytest.mark.parametrize("name", ["enrichment", "rag"])
def test_gating_cases_hold(name: str, request: pytest.FixtureRequest) -> None:
    # Then
    assert gate_failures(fixture_report(request, name)) == []


def test_stop_work_cases_are_gating_and_held(rag: Report) -> None:
    # Given
    cases = section(rag, "cases", "cases")

    # Then
    for name in (
        "cancel_before_start",
        "cancel_during_model_call",
        "budget_context_too_small",
        "concurrency_capacity_full",
        "storage_outage_and_recovery",
        "enrichment_outage_keeps_lexical",
    ):
        case = section(cases, name)
        assert case["gating"] is True
        assert case["held"] is True, case


def test_network_guard_blocks_and_counts_connects() -> None:
    # When
    with no_network() as attempts:
        with pytest.raises(NetworkBlockedError):
            _ = socket.create_connection(("192.0.2.1", 443), timeout=0.1)
        with socket.socket() as raw, pytest.raises(NetworkBlockedError):
            raw.connect(("192.0.2.1", 443))

    # Then
    assert attempts.count == 2


def test_default_runtime_selects_fake_synthesizer() -> None:
    # When
    runtime = compose_briefing(fixture_store())

    # Then
    assert isinstance(
        runtime.use_case.services.synthesizer.model, DeterministicFakeSynthesizer
    )
    assert runtime.use_case.activation.mode == "fixture"


def test_metric_helpers_handle_empty_samples() -> None:
    # Then
    assert ratio(0, 0, "x")["value"] is None
    assert ratio(1, 4, "x")["value"] == 0.25
    assert percentile([], 95) is None
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0
    assert calibration([])["ece"] is None
    perfect = calibration([(1.0, True), (0.0, False)])
    assert (perfect["brier"], perfect["ece"]) == (0.0, 0.0)


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_benchmark_keeps_every_question_inside_its_scope(
    rag: Report, mode: str
) -> None:
    # Given
    per_question = section(rag, "modes", mode, "per_question")
    summary = section(rag, "modes", mode, "summary")

    # Then: nothing retrieved, sent to the model or cited outside the scope.
    for question in per_question:
        assert section(per_question, question, "scope_violations") == {
            "retrieved": [],
            "sent_to_model": [],
            "cited": [],
        }
    compliance = section(summary, "scope_compliance")
    assert (compliance["numerator"], compliance["denominator"]) == (10, 10)
    assert summary["scope_violations_total"] == 0


def test_span_level_gold_metric_is_reported_beside_article_level(rag: Report) -> None:
    # Then
    summary = section(rag, "modes", "hybrid", "summary")
    for key in (
        "citation_gold_article_precision",
        "citation_gold_span_exact_precision",
        "citation_gold_span_overlap_precision",
        "gold_span_coverage",
    ):
        assert isinstance(section(summary, key)["denominator"], int)
    assert section(summary, "gold_span_coverage")["denominator"] == 12


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_ranking_comparison_searches_permitted_distractors(
    rag: Report, mode: str
) -> None:
    # Given: one analyst scope (29 permitted articles, 014 excluded).
    rows = section(rag, "ranking", mode, "per_question")
    summary = section(rag, "ranking", mode, "summary")

    # Then
    assert section(rag, "sample")["ranking_scope_articles"] == 29
    assert section(rag, "sample")["ranking_scope_restricted_excluded"] == [
        "00000000-0000-4000-8000-000000000014"
    ]
    assert len(rows) == 10
    assert summary["scope_violations_total"] == 0
    cited = {
        str(c)
        for q in rows
        for c in cast("list[Json]", section(rows, q).get("cited_articles", []))
    }
    assert "00000000-0000-4000-8000-000000000014" not in cited
    assert len(cited) > 12  # distractors are reachable, not pre-filtered away
    for key in ("mrr", "ndcg_at_10"):
        assert section(summary, key)["questions"] == 8


def test_measured_report_times_every_pass_including_ranking() -> None:
    # When
    measured = rag_evaluate(load_corpus(), repeats=1)[1]

    # Then: no execution error, and exactly one wall and one overhead sample
    # per question in each of the four passes (abstentions included).
    for path in (
        ("lexical",),
        ("hybrid",),
        ("ranking", "lexical"),
        ("ranking", "hybrid"),
    ):
        timings = section(measured, *path)
        assert timings["execution_errors"] == 0
        assert section(timings, "end_to_end")["samples"] == 10
        assert section(timings, "graph_overhead")["samples"] == 10
