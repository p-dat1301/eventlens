"""Benchmark pipeline configurations (``name#profile``): A/B/C, keys, no leakage."""

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.search_eval.dataset import SearchDataset, load_dataset
from backend.rag.evaluation.search_eval.factory import (
    SearchEvalConfigError,
    SystemOptions,
    build_systems,
)
from backend.rag.evaluation.search_eval.pipeline import PipelineSystem
from backend.rag.evaluation.search_eval.runner import evaluate

ROOT = Path(__file__).parents[4]


@pytest.fixture(scope="module")
def dataset() -> SearchDataset:
    return load_dataset()


def _systems(*configs: str) -> list[PipelineSystem]:
    built = build_systems(SystemOptions(configs=configs), None)
    assert all(isinstance(s, PipelineSystem) for s in built)
    return [s for s in built if isinstance(s, PipelineSystem)]


class _Keys:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def load(self, name: str, key: str) -> tuple[Json, Json] | None:
        _ = (name, key)
        return None

    def save(self, name: str, key: str, entry: Json, measured: Json) -> None:
        _ = (entry, measured)
        self.keys[name] = key


def test_pipeline_reports_candidate_selection_and_payload_stages(
    dataset: SearchDataset,
) -> None:
    report, measured = evaluate(
        dataset, _systems("memory-hybrid#baseline", "memory-hybrid#div3"), 3, {}
    )

    configs = report["configurations"]
    assert isinstance(configs, dict)
    for name in ("memory-hybrid#baseline", "memory-hybrid#div3"):
        entry = configs[name]
        assert isinstance(entry, dict)
        evidence = entry["evidence"]
        assert isinstance(evidence, dict)
        assert {"selected", "payload", "overlap_dropped", "branch_cap_dropped"} <= set(
            evidence
        )
        assert entry["errors"] == []
        assert entry["payload_repeat_consistency"] == {
            "definition": "questions whose payload spans are identical in every repeat",
            "denominator": 40,
            "numerator": 40,
            "value": 1.0,
        }
        row = entry["per_question"]
        assert isinstance(row, list)
        assert isinstance(row[0], dict)
        assert "c_gold_frac" in row[0]
        configuration = entry["configuration"]
        assert isinstance(configuration, dict)
        assert configuration["context_budget"] == 12_000
        assert configuration["result_limit"] == 5
    latency = measured["configurations"]
    assert isinstance(latency, dict)
    stages = latency["memory-hybrid#div3"]
    assert isinstance(stages, dict)
    assert "stage:assemble" in str(stages["latency"])


def test_checkpoint_keys_differ_for_every_profile(dataset: SearchDataset) -> None:
    keys = _Keys()
    small = replace(dataset, questions=dataset.questions[:4])
    profiles = ("baseline", "div2", "div3", "w1-2", "fresh7", "boost5-2-1")

    _ = evaluate(
        small,
        _systems(*(f"memory-hybrid#{p}" for p in profiles)),
        3,
        {},
        checkpoints=keys,
    )

    assert len(set(keys.keys.values())) == len(profiles)


def test_runtime_never_sees_labels(dataset: SearchDataset) -> None:
    # Given: the same query and filters with contradicting gold labels.
    question = next(q for q in dataset.questions if q.answerable)
    relabelled = replace(
        question,
        question_id="relabelled",
        required_article_ids=(uuid4(),),
        article_grades={uuid4(): 2},
        passages=(),
        category="multi_source",
        split="dev",
    )
    (system,) = _systems("memory-hybrid#div3")
    _ = system.prepare(dataset.articles)
    try:
        # When
        left = system.search(question)
        right = system.search(relabelled)
    finally:
        system.close()

    # Then: identical candidates and payload; labels only change scores.
    assert left.ranked == right.ranked
    assert left.evidence is not None
    assert right.evidence is not None
    assert left.evidence.payload == right.evidence.payload


def test_unknown_profile_and_reranked_pipeline_are_refused() -> None:
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(SystemOptions(configs=("memory-hybrid#nope",)), None)
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(
            SystemOptions(configs=("memory-hybrid-rerank#baseline",), rerank="fake"),
            None,
        )


FRESHNESS = ROOT / "fixtures/rag/search-selection/freshness-updates-v1"


def test_freshness_fixture_is_reproducible_and_has_a_fixed_clock() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/rag/build_freshness_fixture.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    updates = load_dataset(FRESHNESS)

    assert completed.returncode == 0, completed.stderr
    assert updates.as_of == datetime(2026, 5, 20, tzinfo=UTC)
    assert {q.category for q in updates.questions} == {
        "update",
        "historical",
        "explicit_window",
    }
    assert any(a.source_published_at is None for a in updates.articles)
    assert any(
        a.source_published_at is not None and a.source_published_at > updates.as_of
        for a in updates.articles
    )


def test_intent_freshness_only_moves_update_questions_on_the_synthetic_set() -> None:
    updates = load_dataset(FRESHNESS)
    systems = build_systems(
        SystemOptions(configs=("memory-hybrid#wrrf-selected", "memory-hybrid#fresh7")),
        None,
        as_of=updates.as_of,
    )

    report, _ = evaluate(updates, systems, 3, {})

    configs = report["configurations"]
    assert isinstance(configs, dict)
    base, fresh = (
        configs["memory-hybrid#wrrf-selected"],
        configs["memory-hybrid#fresh7"],
    )
    assert isinstance(base, dict)
    assert isinstance(fresh, dict)
    base_metrics, fresh_metrics = base["metrics"], fresh["metrics"]
    assert isinstance(base_metrics, dict)
    assert isinstance(fresh_metrics, dict)
    before, after = base_metrics["freshness@1"], fresh_metrics["freshness@1"]
    assert isinstance(before, dict)
    assert isinstance(after, dict)
    assert int(str(after["numerator"])) > int(str(before["numerator"]))
    evidence = fresh["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["freshness_decisions"] == {
        "applied": 6,
        "explicit_window": 2,
        "intent_not_eligible": 4,
    }
    rows = {
        str(r["question_id"]): r
        for r in cast("list[dict[str, Json]]", fresh["per_question"])
    }
    base_rows = {
        str(r["question_id"]): r
        for r in cast("list[dict[str, Json]]", base["per_question"])
    }
    for qid in ("f-07", "f-08", "f-09", "f-10", "f-11", "f-12"):
        assert rows[qid]["top_articles"] == base_rows[qid]["top_articles"]
    assert fresh_metrics["scope_violations"] == 0


def test_trace_reconstructs_every_layer_of_the_returned_answer(
    dataset: SearchDataset,
) -> None:
    (system,) = _systems("memory-hybrid#baseline")
    _ = system.prepare(dataset.articles)
    try:
        for question in dataset.questions[:12]:
            answer = system.search(question)
            evidence = answer.evidence
            assert evidence is not None
            trace = evidence.trace
            assert trace is not None
            fused = cast("list[list[Json]]", trace["fused"])
            # A: the fused list is exactly the ranked list the scores use.
            assert [(f[0], f[1], f[2]) for f in fused] == [
                (str(p.article_id), p.char_start, p.char_end) for p in answer.ranked
            ]
            # B: the first result_limit groups are the selected spans.
            limit = cast("int", trace["result_limit"])
            groups = cast("list[list[Json]]", trace["groups"])
            selected = [
                (g[0], s[0], s[1])
                for g in groups[:limit]
                for s in cast("list[list[int]]", g[1])
            ]
            assert selected == [
                (s.article_id, s.start, s.end) for s in evidence.selected
            ]
            # C: payload spans equal the stage-C spans the metrics score.
            payload = cast("list[list[Json]]", trace["payload"])
            assert [(p[0], p[1], p[2]) for p in payload] == [
                (s.article_id, s.start, s.end) for s in evidence.payload
            ]
            branches = cast("dict[str, list[list[Json]]]", trace["branches"])
            # Every fused candidate came from a raw branch hit.
            raw = {
                (h[0], h[1], h[2])
                for name in ("lexical", "semantic")
                for h in branches.get(name, [])
            }
            assert {(f[0], f[1], f[2]) for f in fused} <= raw
            counters = cast("dict[str, int]", trace["counters"])
            assert counters["payload_tokens"] == evidence.payload_tokens
            assert counters["selection_cap_dropped"] == evidence.cap_dropped
            # Query embedding is timed separately and is part of the total.
            timings = cast("dict[str, float]", trace["timings_ms"])
            assert 0.0 <= timings["embed_query"] <= timings["total"]
            assert "embed_query" in answer.timings_ms
            text = str(trace)
            assert question.query not in text  # text-free
    finally:
        system.close()


def test_measured_report_keeps_latency_per_repeat(dataset: SearchDataset) -> None:
    _, measured = evaluate(dataset, _systems("memory-bm25#baseline"), 3, {})

    configs = measured["configurations"]
    assert isinstance(configs, dict)
    entry = configs["memory-bm25#baseline"]
    assert isinstance(entry, dict)
    by_repeat = entry["latency_by_repeat"]
    assert isinstance(by_repeat, list)
    assert len(by_repeat) == 3
    for repeat in by_repeat:
        assert isinstance(repeat, dict)
        total = repeat["total"]
        assert isinstance(total, dict)
        assert total["samples"] == len(dataset.questions)
