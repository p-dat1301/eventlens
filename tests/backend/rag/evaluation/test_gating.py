"""Fail-closed eval gates: missing or malformed sections never pass."""

import copy
from collections.abc import Callable
from typing import cast

import pytest

from backend.rag.evaluation.corpus import load_corpus
from backend.rag.evaluation.enrichment_cases import CASES as LIFECYCLE_CASES
from backend.rag.evaluation.enrichment_eval import evaluate as enrichment_evaluate
from backend.rag.evaluation.gating import (
    REQUIRED_LIFECYCLE,
    REQUIRED_RAG_GATING,
    REQUIRED_RAG_INFORMATIONAL,
    gate_failures,
)
from backend.rag.evaluation.metrics import Json, digest
from backend.rag.evaluation.rag_cases import CASES as RAG_CASES
from backend.rag.evaluation.rag_cases import INFORMATIONAL
from backend.rag.evaluation.rag_eval import evaluate as rag_evaluate

type Report = dict[str, Json]
type Mutation = Callable[[Report], None]


@pytest.fixture(scope="module")
def enrichment() -> Report:
    return enrichment_evaluate(load_corpus())[0]


@pytest.fixture(scope="module")
def rag() -> Report:
    return rag_evaluate(load_corpus(), repeats=1)[0]


def node(report: Report, *path: str) -> dict[str, Json]:
    current: Json = report
    for key in path:
        assert isinstance(current, dict)
        current = current[key]
    assert isinstance(current, dict)
    return current


def resigned(report: Report, mutate: Mutation) -> Report:
    """Deep copy, mutate, then recompute the digest (isolates the check)."""
    changed = copy.deepcopy(report)
    _ = changed.pop("report_digest", None)
    mutate(changed)
    changed["report_digest"] = digest(cast("Json", changed))
    return changed


def test_required_case_lists_match_the_case_registries() -> None:
    # Then: dropping a case from code also fails this test, not only the gate.
    assert set(LIFECYCLE_CASES) == REQUIRED_LIFECYCLE
    assert set(RAG_CASES) == REQUIRED_RAG_GATING | REQUIRED_RAG_INFORMATIONAL
    assert INFORMATIONAL == REQUIRED_RAG_INFORMATIONAL


@pytest.mark.parametrize(
    "minimal",
    [
        {},
        {"schema_version": "enrichment_eval_report.v1"},
        {"schema_version": "rag_eval_report.v1"},
        {"schema_version": "rag_eval_report.v1", "modes": {}, "safety": {}},
        {"schema_version": "unknown.v9"},
    ],
)
def test_minimal_reports_fail(minimal: Report) -> None:
    # Given
    report = resigned(minimal, lambda _: None)

    # Then
    assert gate_failures(report) != []


def drop_lifecycle(r: Report) -> None:
    del r["lifecycle_cases"]


def drop_one_lifecycle_case(r: Report) -> None:
    del node(r, "lifecycle_cases", "cases")["retry_exhaustion"]


def malformed_lifecycle_entry(r: Report) -> None:
    node(r, "lifecycle_cases", "cases")["retry_exhaustion"] = "held"


def lying_held_flag(r: Report) -> None:
    entry = node(r, "lifecycle_cases", "cases", "retry_exhaustion")
    node(entry, "observed")["provider_calls"] = 99


def drop_lifecycle_gating_flag(r: Report) -> None:
    del node(r, "lifecycle_cases", "cases", "retry_exhaustion")["gating"]


def enrichment_network(r: Report) -> None:
    node(r, "safety")["network_connect_attempts"] = 1


def drop_enrichment_safety(r: Report) -> None:
    del r["safety"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (drop_lifecycle, "case table missing or malformed"),
        (drop_one_lifecycle_case, "required case retry_exhaustion missing"),
        (malformed_lifecycle_entry, "case retry_exhaustion malformed"),
        (lying_held_flag, "case retry_exhaustion held flag disagrees"),
        (enrichment_network, "safety network_connect_attempts=1"),
        (drop_enrichment_safety, "safety section missing or malformed"),
        (
            drop_lifecycle_gating_flag,
            "case retry_exhaustion lacks a boolean gating flag",
        ),
    ],
)
def test_enrichment_gate_fails_closed(
    enrichment: Report, mutate: Mutation, message: str
) -> None:
    # When
    failures = gate_failures(resigned(enrichment, mutate))

    # Then
    assert any(f.startswith(message) for f in failures), failures


def drop_rag_cases(r: Report) -> None:
    del r["cases"]


def drop_one_rag_case(r: Report) -> None:
    del node(r, "cases", "cases")["access_revoked"]


def drop_informational_case(r: Report) -> None:
    del node(r, "cases", "cases")["scoped_abstention"]


def failed_rag_case(r: Report) -> None:
    entry = node(r, "cases", "cases", "access_revoked")
    node(entry, "observed")["model_calls"] = 1
    entry["held"] = False


def informational_marked_passing_gate(r: Report) -> None:
    node(r, "cases", "cases", "access_revoked")["gating"] = False


def drop_repeat_consistency(r: Report) -> None:
    del node(r, "sample")["repeat_consistent"]


def inconsistent_repeat(r: Report) -> None:
    node(r, "sample", "repeat_consistent")["hybrid"] = False


def scope_violation(r: Report) -> None:
    node(r, "modes", "lexical", "summary")["scope_violations_total"] = 1


def drop_mode(r: Report) -> None:
    del node(r, "modes")["hybrid"]


Q01 = "40000000-0000-4000-8000-000000000001"
RESTRICTED = "00000000-0000-4000-8000-000000000014"


def row_cites_out_of_scope(r: Report) -> None:
    row = node(r, "modes", "hybrid", "per_question", Q01, "scope_violations")
    row["cited"] = [RESTRICTED]


def drop_most_rows(r: Report) -> None:
    rows = node(r, "modes", "lexical", "per_question")
    for qid in sorted(rows)[1:]:
        del rows[qid]


def malformed_row(r: Report) -> None:
    del node(r, "ranking", "hybrid", "per_question", Q01)["scope_violations"]


def ranking_row_leak(r: Report) -> None:
    row = node(r, "ranking", "lexical", "per_question", Q01, "scope_violations")
    row["sent_to_model"] = [RESTRICTED]
    node(r, "ranking", "lexical", "summary")["scope_violations_total"] = 1


def compliance_lie(r: Report) -> None:
    node(r, "ranking", "hybrid", "summary", "scope_compliance")["numerator"] = 9


def drop_ranking(r: Report) -> None:
    del node(r, "ranking")["lexical"]


def drop_rag_gating_flag(r: Report) -> None:
    del node(r, "cases", "cases", "access_revoked")["gating"]


def drop_ranking_repeat_flag(r: Report) -> None:
    del node(r, "sample", "repeat_consistent")["ranking_hybrid"]


def ranking_row_execution_error(r: Report) -> None:
    rows = node(r, "ranking", "hybrid", "per_question")
    rows[Q01] = {
        "route": "error",
        "error_code": "retrieval_storage_unavailable",
        "scope_violations": {"retrieved": [], "sent_to_model": [], "cited": []},
    }


def lexical_row_budget_error(r: Report) -> None:
    rows = node(r, "modes", "lexical", "per_question")
    rows[Q01] = {
        "route": "error",
        "error_code": "synthesis_budget_exceeded",
        "scope_violations": {"retrieved": [], "sent_to_model": [], "cited": []},
    }


def live_default(r: Report) -> None:
    node(r, "safety")["default_model"] = "OpenAiSynthesizer"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (drop_rag_cases, "case table missing or malformed"),
        (drop_one_rag_case, "required case access_revoked missing"),
        (drop_informational_case, "required case scoped_abstention missing"),
        (failed_rag_case, "case access_revoked did not hold"),
        (informational_marked_passing_gate, "case access_revoked gating flag"),
        (drop_repeat_consistency, "sample.repeat_consistent missing"),
        (inconsistent_repeat, "repeat inconsistency in hybrid"),
        (scope_violation, "modes.lexical summary scope_violations_total 1"),
        (drop_mode, "modes.hybrid missing or malformed"),
        (row_cites_out_of_scope, "modes.hybrid row " + Q01 + " scope violations: 1"),
        (row_cites_out_of_scope, "modes.hybrid summary scope_violations_total 0"),
        (drop_most_rows, "modes.lexical question IDs differ"),
        (malformed_row, "ranking.hybrid row " + Q01 + " malformed"),
        (ranking_row_leak, "ranking.lexical row " + Q01 + " scope violations: 1"),
        (compliance_lie, "ranking.hybrid summary scope_compliance disagrees"),
        (drop_ranking, "ranking.lexical missing or malformed"),
        (drop_rag_gating_flag, "case access_revoked lacks a boolean gating flag"),
        (drop_ranking_repeat_flag, "sample.repeat_consistent missing"),
        (
            ranking_row_execution_error,
            "ranking.hybrid row " + Q01 + " route 'error' is not a completed",
        ),
        (lexical_row_budget_error, "modes.lexical row " + Q01 + " route 'error'"),
        (live_default, "safety default_model='OpenAiSynthesizer'"),
    ],
)
def test_rag_gate_fails_closed(rag: Report, mutate: Mutation, message: str) -> None:
    # When
    failures = gate_failures(resigned(rag, mutate))

    # Then
    assert any(f.startswith(message) for f in failures), failures


def test_tampered_report_without_new_digest_fails(rag: Report) -> None:
    # Given
    tampered = copy.deepcopy(rag)
    node(tampered, "safety")["network_connect_attempts"] = 0.0

    # Then
    assert "report_digest missing or wrong" in gate_failures(tampered)


def test_quality_metric_changes_never_gate(rag: Report) -> None:
    # Given
    def worse(r: Report) -> None:
        node(r, "modes", "hybrid", "summary", "recall_at_5")["value"] = 0.0
        node(r, "modes", "hybrid", "summary", "abstention_correct")["value"] = 0.0

    # Then
    assert gate_failures(resigned(rag, worse)) == []


@pytest.mark.parametrize("name", ["enrichment", "rag"])
def test_real_reports_pass(name: str, request: pytest.FixtureRequest) -> None:
    # Given
    value: object = request.getfixturevalue(name)  # pyright: ignore[reportAny]
    assert isinstance(value, dict)

    # Then
    assert gate_failures(cast("Report", value)) == []


def test_valid_abstention_rows_pass_the_gate(rag: Report) -> None:
    # Given: the real report holds a legitimate abstention (Q007, lexical scope).
    rows = node(rag, "modes", "lexical", "per_question")
    routes = {str(node(rows, q)["route"]) for q in rows}

    # Then
    assert "abstained_no_evidence" in routes
    assert gate_failures(rag) == []
