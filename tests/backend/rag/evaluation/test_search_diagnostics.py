"""Layer diagnostics: facet coverage, attribution per layer, judged share, CIs."""

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.search_eval.diagnostics import (
    DiagnosticsInputError,
    Facet,
    QuestionLabel,
    Trace,
    aggregate,
    attribute,
    check_identities,
    collect_traces,
    contained_in_one,
    covered,
    load_labels,
    paired_clustered,
    parse_trace,
    question_row,
    satisfied,
)

A: str = "art-a"
B: str = "art-b"
C: str = "art-c"


def _trace(  # noqa: PLR0913 - one trace per test, every layer explicit
    *,
    lexical: list[list[object]] | None = None,
    fused: list[list[object]] | None = None,
    groups: list[list[object]] | None = None,
    payload: list[list[object]] | None = None,
    counters: dict[str, int] | None = None,
    result_limit: int = 2,
) -> Trace:
    return parse_trace(
        {
            "branches": {"lexical": lexical or [], "semantic": []},
            "fused": fused or [],
            "groups": groups or [],
            "result_limit": result_limit,
            "max_spans_per_article": 2,
            "payload": payload or [],
            "counters": counters or {},
        }
    )


def _label(
    facets: tuple[Facet, ...],
    *,
    grades: dict[str, int] | None = None,
    eligible: bool = True,
    answerable: bool = True,
) -> QuestionLabel:
    return QuestionLabel(
        question_id="q",
        split="dev",
        category="multi_source",
        eligible=eligible,
        answerable=answerable,
        no_answer_kind=None,
        grades=grades if grades is not None else {A: 2, B: 2},
        gold_spans=tuple(s for f in facets for alt in f.alternatives for s in alt),
        facets=facets,
        filters={},
        group="G1",
    )


CORPUS: dict[str, tuple[str, datetime | None]] = dict.fromkeys(
    (A, B, C), ("x.vn", None)
)


def test_gold_span_split_across_overlapping_chunks_is_covered_not_contained() -> None:
    stage = [(A, 0, 120), (A, 100, 220)]
    assert covered((A, 90, 150), stage)
    assert not contained_in_one((A, 90, 150), stage)
    assert not covered((A, 90, 250), stage)
    assert not covered((B, 90, 150), stage)


def test_alternatives_are_substitutes_and_spans_inside_one_are_complements() -> None:
    facet = Facet(
        "f", required=True, alternatives=(((A, 0, 10), (A, 50, 60)), ((B, 0, 10),))
    )
    assert satisfied(facet, [(B, 0, 20)])  # a re-post is enough
    assert not satisfied(facet, [(A, 0, 20)])  # half of a complementary pair
    assert satisfied(facet, [(A, 0, 20), (A, 40, 70)])


def test_each_layer_loss_gets_its_own_class() -> None:
    facet = Facet("f", required=True, alternatives=(((A, 0, 10),),))
    nowhere = _trace(lexical=[[B, 0, 50, 1, False]])
    assert attribute(facet, nowhere)["class"] == "CANDIDATE_MISS"
    assert attribute(facet, nowhere)["reason"] == "article_not_in_any_branch_candidate"
    other_window = _trace(lexical=[[A, 300, 350, 1, False]])
    assert (
        attribute(facet, other_window)["reason"]
        == "article_in_candidates_but_needed_chunk_not"
    )
    cut = _trace(lexical=[[A, 0, 50, 1, False]], fused=[[B, 0, 50, 1, None]])
    assert attribute(facet, cut)["reason"] == "article_cut_before_fused_list"
    ranked_low = _trace(
        lexical=[[A, 0, 50, 3, False]],
        fused=[[B, 0, 50, 1, None], [C, 0, 50, 2, None], [A, 0, 50, 3, None]],
        groups=[[B, [[0, 50]]], [C, [[0, 50]]], [A, [[0, 50]]]],
    )
    loss = attribute(facet, ranked_low)
    assert loss["class"] == "RANKING_MISS"
    assert loss["reason"] == "article_rank_3_beyond_limit_2"
    capped = _trace(
        lexical=[
            [A, 100, 150, 1, False],
            [A, 200, 250, 2, False],
            [A, 0, 50, 3, False],
        ],
        fused=[[A, 100, 150, 1, None], [A, 200, 250, 2, None], [A, 0, 50, 3, None]],
        groups=[[A, [[100, 150], [200, 250]]]],
    )
    assert attribute(facet, capped)["reason"] == "span_cap_per_article"
    budget = _trace(
        lexical=[[A, 0, 50, 1, False]],
        fused=[[A, 0, 50, 1, None]],
        groups=[[A, [[0, 50]]]],
        payload=[[B, 0, 5, False]],
    )
    assert attribute(facet, budget)["class"] == "BUDGET_TRUNCATION"
    dropped = _trace(
        lexical=[[A, 0, 50, 1, False]],
        fused=[[A, 0, 50, 1, None]],
        groups=[[A, [[0, 50]]]],
        counters={"dropped_at_final_check": 1},
    )
    assert attribute(facet, dropped)["class"] == "SCOPE_OR_REVISION_FAILURE"


def test_trap_hits_are_counted_and_never_used_as_candidates() -> None:
    trace = _trace(lexical=[[A, 0, 50, 1, True], [B, 0, 50, 2, False]])
    assert trace.trap_hits == 1
    assert trace.layer("branch") == ((B, 0, 50),)


def test_question_row_keeps_unjudged_apart_from_grade0_and_names_the_class() -> None:
    facets = (
        Facet("f1", required=True, alternatives=(((A, 0, 10),),)),
        Facet("f2", required=True, alternatives=(((B, 0, 10),),)),
    )
    label = _label(facets, grades={A: 2, B: 2, C: 0})
    trace = _trace(
        lexical=[[A, 0, 50, 1, False], [C, 0, 50, 2, False], ["u", 0, 9, 3, False]],
        fused=[[A, 0, 50, 1, None], [C, 0, 50, 2, None], ["u", 0, 9, 3, None]],
        groups=[[A, [[0, 50]]], [C, [[0, 50]]]],
        payload=[[A, 0, 50, False], [C, 0, 50, False]],
    )
    row = question_row(label, trace, {**CORPUS, "u": ("x.vn", None)})
    assert row["judged@5"] == round(2 / 3, 4)  # C is judged grade 0, u is unjudged
    assert row["recall@5"] == 0.5
    assert row["facets_payload"] == 1
    assert row["complete_payload"] is False
    assert row["class"] == "CANDIDATE_MISS"
    assert row["possible_unjudged_alternative"] is False
    unresolved = question_row(_label(facets, eligible=False), trace, CORPUS)
    assert unresolved["class"] == "LABEL_UNRESOLVED"
    no_answer = question_row(_label((), answerable=False, grades={}), trace, CORPUS)
    assert no_answer["class"] == "NO_ANSWER"
    complete = question_row(_label(facets[:1], grades={A: 2}), trace, CORPUS)
    assert complete["class"] == "EVIDENCE_COMPLETE"
    summary = aggregate([row, complete])
    coverage = cast("dict[str, Json]", summary["facet_coverage_payload"])
    assert (coverage["numerator"], coverage["denominator"]) == (2, 3)


def test_payload_outside_the_question_filter_is_a_violation() -> None:
    facets = (Facet("f1", required=True, alternatives=(((A, 0, 10),),)),)
    label = QuestionLabel(
        question_id="q",
        split="dev",
        category="filter",
        eligible=True,
        answerable=True,
        no_answer_kind=None,
        grades={A: 2},
        gold_spans=((A, 0, 10),),
        facets=facets,
        filters={"source_domains": ["tuoitre.vn"]},
        group="G1",
    )
    trace = _trace(payload=[[A, 0, 50, False]])
    row = question_row(label, trace, {A: ("vietstock.vn", None)})
    assert row["scope_or_stale_payload_articles"] == [A]


def test_cluster_bootstrap_resamples_groups_together() -> None:
    diffs = {"q1": 1.0, "q2": 1.0, "q3": 0.0, "q4": -1.0}
    groups = {"q1": "G1", "q2": "G1", "q3": "G2", "q4": "G3"}
    first = paired_clustered(diffs, groups, samples=2000)
    again = paired_clustered(diffs, groups, samples=2000)
    assert first == again  # seeded
    assert first["groups"] == 3
    assert first["n"] == 4
    assert first["mean_diff"] == 0.25
    low, high = cast("list[float]", first["ci95_cluster_bootstrap"])
    assert low <= 0.25 <= high
    single = paired_clustered(
        {"q1": 1.0, "q2": 1.0}, {"q1": "G", "q2": "G"}, samples=200
    )
    assert single["ci95_cluster_bootstrap"] == [1.0, 1.0]


def test_an_empty_alternative_never_satisfies_a_facet() -> None:
    facet = Facet("f", required=True, alternatives=(((A, 0, 10),), ()))
    assert not satisfied(facet, [])
    assert not satisfied(facet, [(B, 0, 50)])
    assert satisfied(facet, [(A, 0, 50)])


def _sidecar(alternatives: list[object]) -> dict[str, object]:
    ref = {"article_id": A, "start": 0, "end": 10, "excerpt_sha256": "x"}
    return {
        "questions": {
            "q1": {
                "facets": [
                    {
                        "facet_id": "q1#F1",
                        "required": True,
                        "alternatives": [[ref], *alternatives],
                    }
                ]
            }
        }
    }


QUESTIONS: dict[str, object] = {
    "questions": [
        {
            "qid": "q1",
            "split": "dev",
            "category": "x",
            "answerable": True,
            "gold": [
                {
                    "article_id": A,
                    "grade": 2,
                    "spans": [{"start": 0, "end": 10, "excerpt_sha256": "x"}],
                }
            ],
        },
        {"qid": "q2", "split": "dev", "category": "x", "answerable": False, "gold": []},
    ]
}
PLAN: dict[str, object] = {"eligible_answerable": ["q1"], "event_groups": {}}


def test_load_labels_rejects_malformed_facets() -> None:
    assert load_labels(QUESTIONS, _sidecar([]), PLAN)["q1"].facets
    with pytest.raises(DiagnosticsInputError, match="facet_empty_alternative"):
        _ = load_labels(QUESTIONS, _sidecar([[]]), PLAN)
    with pytest.raises(DiagnosticsInputError, match="without facets"):
        _ = load_labels(QUESTIONS, {"questions": {}}, PLAN)


def _line(config: str, qid: str, **extra: object) -> str:
    trace: dict[str, object] = {
        "branches": {"lexical": [], "semantic": []},
        "fused": [],
        "groups": [],
        "result_limit": 5,
        "max_spans_per_article": 3,
        "payload": [],  # an empty payload (abstention) is a valid record
        "counters": {},
    }
    return json.dumps(
        {"configuration": config, "question_id": qid, "trace": trace, **extra}
    )


def test_collect_traces_requires_every_configuration_question_pair() -> None:
    labels = load_labels(QUESTIONS, _sidecar([]), PLAN)
    full = [_line(c, q) for c in ("X", "Y") for q in ("q1", "q2")]
    traced = collect_traces(full, labels, ["X", "Y"])
    assert {c: sorted(v) for c, v in traced.items()} == {
        "X": ["q1", "q2"],
        "Y": ["q1", "q2"],
    }
    bad: list[tuple[list[str], str]] = [
        (full[:1], "records missing"),  # truncated file
        (full[:2], "records missing"),  # one configuration absent
        ([*full, full[0]], "duplicate"),
        ([*full, _line("X", "q3")], "unknown question"),
        ([*full, _line("Z", "q1")], "not in the manifest"),
        ([_line("X", "q1", repeat=2), *full[1:]], "repeat"),
        (["{not json", *full], "invalid JSON"),
        (
            [json.dumps({"configuration": "X", "question_id": "q1"}), *full[1:]],
            "misses",
        ),
        (
            [
                json.dumps({"configuration": "X", "question_id": "q1", "trace": {}}),
                *full[1:],
            ],
            "trace misses",
        ),
    ]
    for lines, message in bad:
        with pytest.raises(DiagnosticsInputError, match=message):
            _ = collect_traces(lines, labels, ["X", "Y"])
    with pytest.raises(DiagnosticsInputError, match="no configurations"):
        _ = collect_traces(full, labels, [])


def test_manifest_identities_must_match_the_inputs() -> None:
    data = b"labels"
    good = {"questions_v2_sha256": hashlib.sha256(data).hexdigest()}
    check_identities(good, {"questions_v2_sha256": data})
    with pytest.raises(DiagnosticsInputError, match="questions_v2_sha256"):
        check_identities(good, {"questions_v2_sha256": b"other"})
