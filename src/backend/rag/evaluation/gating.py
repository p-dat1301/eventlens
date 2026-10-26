"""Deterministic safety/control gates over a finished eval report.

Fail closed: the report must match a known schema version, carry every
required section and every required case in the expected shape, and its
``report_digest`` must match its content. A missing or malformed section is a
failure, never a silent pass. Held is recomputed from ``expected ==
observed`` rather than trusted. Quality metrics (precision, recall,
abstention, conflict detection) are reported only and never compared against
a threshold here.
"""

from .corpus import load_corpus
from .metrics import Json, digest

ENRICHMENT_SCHEMA = "enrichment_eval_report.v1"
RAG_SCHEMA = "rag_eval_report.v1"
REQUIRED_LIFECYCLE = frozenset(
    {
        "unsupported_taxonomy",
        "ungrounded_entity",
        "missing_evidence_reference",
        "poisoned_suggested_tag",
        "timeout_then_success",
        "retry_exhaustion",
        "stale_revision_mid_call",
        "cancel_stops_further_work",
    }
)
REQUIRED_RAG_GATING = frozenset(
    {
        "restricted_article_never_sent",
        "prompt_injection_data_channel",
        "model_cites_unsent_restricted_span",
        "model_returns_raw_prose",
        "access_revoked",
        "cancel_before_start",
        "cancel_during_model_call",
        "budget_context_too_small",
        "concurrency_capacity_full",
        "concurrency_isolation",
        "storage_outage_and_recovery",
        "enrichment_outage_keeps_lexical",
        "model_outage_typed_no_fallback",
        "freshness_revision",
        "freshness_tombstone",
    }
)
# Answer-quality expectations of the fake model: must be present, never gate.
REQUIRED_RAG_INFORMATIONAL = frozenset({"scoped_abstention"})
EXPECTED_RAG_SAFETY: dict[str, Json] = {
    "default_model": "DeterministicFakeSynthesizer",
    "activation_mode": "fixture",
    "graph_checkpointer": "None",
    "network_connect_attempts": 0,
}
EXPECTED_ENRICHMENT_SAFETY: dict[str, Json] = {
    "enricher": "FakeArticleEnricher",
    "network_connect_attempts": 0,
}
MODES = ("lexical", "hybrid")
PASSES = ("lexical", "hybrid", "ranking_lexical", "ranking_hybrid")
MODE_PATHS = tuple(
    f"{section}.{mode}" for section in ("modes", "ranking") for mode in MODES
)
STAGES = ("retrieved", "sent_to_model", "cited")
# A benchmark row must be a completed briefing: answered or a valid abstention.
# Execution errors (route "error": storage, budget, provider, ...) fail the gate.
COMPLETED_ROUTES = frozenset(
    {"answered", "abstained_no_evidence", "abstained_by_model"}
)
NO_CASES: frozenset[str] = frozenset()


def _section(node: Json, *path: str) -> dict[str, Json] | None:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def _case_problem(name: str, entry: Json, *, gating: bool | None) -> str | None:
    if not isinstance(entry, dict):
        return f"case {name} malformed"
    expected, observed = entry.get("expected"), entry.get("observed")
    is_gating = entry.get("gating")
    problems = (
        (not isinstance(is_gating, bool), f"case {name} lacks a boolean gating flag"),
        (
            not isinstance(expected, dict) or not isinstance(observed, dict),
            f"case {name} lacks expected/observed objects",
        ),
        (not isinstance(entry.get("held"), bool), f"case {name} lacks a boolean held"),
        (
            gating is not None and is_gating is not gating,
            f"case {name} gating flag is {is_gating!r}, required {gating!r}",
        ),
        (
            entry.get("held") is not (expected == observed),
            f"case {name} held flag disagrees with expected/observed",
        ),
        (is_gating is True and expected != observed, f"case {name} did not hold"),
    )
    return next((message for failed, message in problems if failed), None)


def _cases(
    table: dict[str, Json] | None,
    gating: frozenset[str],
    informational: frozenset[str] = NO_CASES,
) -> list[str]:
    cases = _section(table, "cases")
    if cases is None:
        return ["case table missing or malformed"]
    missing = sorted((gating | informational) - cases.keys())
    failures = [f"required case {name} missing" for name in missing]
    for name, entry in sorted(cases.items()):
        flag = True if name in gating else False if name in informational else None
        problem = _case_problem(name, entry, gating=flag)
        if problem is not None:
            failures.append(problem)
    return failures


def _safety(report: dict[str, Json], expected: dict[str, Json]) -> list[str]:
    safety = _section(report, "safety")
    if safety is None:
        return ["safety section missing or malformed"]
    return [
        f"safety {key}={safety.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if safety.get(key) != value or type(safety.get(key)) is not type(value)
    ]


def _row_violations(row: Json) -> int | None:
    """Out-of-scope IDs in one per-question row; ``None`` if malformed."""
    stages = _section(row, "scope_violations")
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("route"), str)
        or stages is None
        or set(stages) != set(STAGES)
    ):
        return None
    total = 0
    for name in STAGES:
        values = stages[name]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            return None
        total += len(values)
    return total


def _mode(report: dict[str, Json], path: str, ids: frozenset[str]) -> list[str]:
    summary = _section(report, *path.split("."), "summary")
    rows = _section(report, *path.split("."), "per_question")
    if summary is None or rows is None:
        return [f"{path} missing or malformed"]
    failures: list[str] = []
    if frozenset(rows) != ids:
        failures.append(f"{path} question IDs differ from the fixture question set")
    counts = {qid: _row_violations(row) for qid, row in rows.items()}
    failures += [f"{path} row {q} malformed" for q, c in counts.items() if c is None]
    failures += [
        f"{path} row {q} route {row.get('route')!r} is not a completed briefing"
        for q, row in rows.items()
        if isinstance(row, dict) and row.get("route") not in COMPLETED_ROUTES
    ]
    failures += [
        f"{path} row {q} scope violations: {c}" for q, c in counts.items() if c
    ]
    recomputed = sum(c or 0 for c in counts.values())
    stated = summary.get("scope_violations_total")
    if type(stated) is not int or stated != recomputed:
        failures.append(f"{path} summary scope_violations_total {stated!r} != rows")
    compliance = _section(summary, "scope_compliance")
    clean = sum(1 for c in counts.values() if c == 0)
    if compliance is None or (
        compliance.get("numerator"),
        compliance.get("denominator"),
    ) != (clean, len(ids)):
        failures.append(f"{path} summary scope_compliance disagrees with rows")
    return failures


def _rag(report: dict[str, Json], ids: frozenset[str]) -> list[str]:
    failures = _cases(
        _section(report, "cases"), REQUIRED_RAG_GATING, REQUIRED_RAG_INFORMATIONAL
    )
    failures += _safety(report, EXPECTED_RAG_SAFETY)
    consistent = _section(report, "sample", "repeat_consistent")
    if consistent is None or set(consistent) != set(PASSES):
        failures.append("sample.repeat_consistent missing or malformed")
    else:
        failures += [
            f"repeat inconsistency in {p}" for p in PASSES if consistent[p] is not True
        ]
    for path in MODE_PATHS:
        failures += _mode(report, path, ids)
    return failures


def fixture_question_ids() -> frozenset[str]:
    """Question IDs of the versioned fixture question set."""
    return frozenset(q.question_id for q in load_corpus().questions)


def gate_failures(
    report: dict[str, Json], question_ids: frozenset[str] | None = None
) -> list[str]:
    """Human-readable failed gates (empty only when every gate held).

    ``question_ids`` defaults to the fixture question set; every mode must
    report exactly those questions.
    """
    stated = report.get("report_digest")
    body = {k: v for k, v in report.items() if k != "report_digest"}
    failures = [] if stated == digest(body) else ["report_digest missing or wrong"]
    match report.get("schema_version"):
        case "enrichment_eval_report.v1":
            failures += _cases(_section(report, "lifecycle_cases"), REQUIRED_LIFECYCLE)
            failures += _safety(report, EXPECTED_ENRICHMENT_SAFETY)
        case "rag_eval_report.v1":
            failures += _rag(report, question_ids or fixture_question_ids())
        case other:
            failures.append(f"unknown report schema {other!r}")
    return failures
